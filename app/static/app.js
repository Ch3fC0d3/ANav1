const state = {
  bootstrap: null,
  currentRecording: null,
  selectedFile: null,
  mediaRecorder: null,
  recordedChunks: [],
  processingStage: "idle",
};

const elements = {
  fileInput: document.getElementById("audio-file"),
  uploadButton: document.getElementById("upload-button"),
  captureMessage: document.getElementById("capture-message"),
  reviewPanel: document.getElementById("review-panel"),
  audioPlayer: document.getElementById("audio-player"),
  rawTranscript: document.getElementById("raw-transcript"),
  correctedTranscript: document.getElementById("corrected-transcript"),
  translationText: document.getElementById("translation-text"),
  translationNotes: document.getElementById("translation-notes"),
  topicTags: document.getElementById("topic-tags"),
  refreshButton: document.getElementById("refresh-button"),
  draftMessage: document.getElementById("draft-message"),
  approveButton: document.getElementById("approve-button"),
  draftExplanation: document.getElementById("draft-explanation"),
  glossaryHits: document.getElementById("glossary-hit-list"),
  exampleHits: document.getElementById("example-hit-list"),
  warnings: document.getElementById("warning-list"),
  confidenceBadge: document.getElementById("confidence-badge"),
  statusBadge: document.getElementById("status-badge"),
  glossaryList: document.getElementById("glossary-list"),
  approvedList: document.getElementById("approved-list"),
  recentList: document.getElementById("recent-list"),
  glossaryForm: document.getElementById("glossary-form"),
  glossaryTerm: document.getElementById("glossary-term"),
  glossaryMeaning: document.getElementById("glossary-meaning"),
  glossaryNotes: document.getElementById("glossary-notes"),
  recordToggle: document.getElementById("record-toggle"),
  recordingState: document.getElementById("recording-state"),
  processTitle: document.getElementById("process-title"),
  processPill: document.getElementById("process-pill"),
  processMessage: document.getElementById("process-message"),
  progressSteps: Array.from(document.querySelectorAll(".progress-step")),
};

const PROCESS_SNAPSHOTS = {
  idle: {
    title: "Ready",
    pill: "Idle",
    pillClass: "pill muted",
    message: "Pick a file or record audio to begin.",
  },
  listening: {
    title: "Listening",
    pill: "Listening",
    pillClass: "pill active",
    message: "Listening to the microphone...",
  },
  uploading: {
    title: "Uploading",
    pill: "Uploading",
    pillClass: "pill active",
    message: "Uploading audio to the app...",
  },
  uploaded: {
    title: "Uploaded",
    pill: "Uploaded",
    pillClass: "pill active",
    message: "Audio uploaded. Ready to transcribe.",
  },
  transcribing: {
    title: "Transcribing",
    pill: "Transcribing",
    pillClass: "pill active",
    message: "Turning audio into a rough phonetic transcript...",
  },
  transcribed: {
    title: "Transcript Ready",
    pill: "Transcript Ready",
    pillClass: "pill active",
    message: "Phonetic transcript is ready. Preparing translation context...",
  },
  translating: {
    title: "Translating",
    pill: "Translating",
    pillClass: "pill active",
    message: "Drafting the English translation...",
  },
  done: {
    title: "Done",
    pill: "Done",
    pillClass: "pill success",
    message: "Draft translation ready for review.",
  },
  approved: {
    title: "Approved",
    pill: "Approved",
    pillClass: "pill success",
    message: "Approved and saved to project memory.",
  },
  error: {
    title: "Needs Attention",
    pill: "Error",
    pillClass: "pill error",
    message: "Processing stopped before it finished cleanly.",
  },
};

function setMessage(element, message) {
  element.textContent = message;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      Accept: "application/json",
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...options.headers,
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

function stepStateMap(stage, recording = state.currentRecording) {
  const stepStates = {
    upload: "pending",
    transcribe: "pending",
    translate: "pending",
  };

  switch (stage) {
    case "uploading":
      stepStates.upload = "active";
      break;
    case "uploaded":
      stepStates.upload = "complete";
      break;
    case "transcribing":
      stepStates.upload = "complete";
      stepStates.transcribe = "active";
      break;
    case "transcribed":
      stepStates.upload = "complete";
      stepStates.transcribe = "complete";
      break;
    case "translating":
      stepStates.upload = "complete";
      stepStates.transcribe = "complete";
      stepStates.translate = "active";
      break;
    case "done":
    case "approved":
      stepStates.upload = "complete";
      stepStates.transcribe = "complete";
      stepStates.translate = "complete";
      break;
    case "error":
      if (recording?.raw_transcript?.trim()) {
        stepStates.upload = "complete";
        stepStates.transcribe = "complete";
        stepStates.translate = "error";
      } else if (recording) {
        stepStates.upload = "complete";
        stepStates.transcribe = "error";
      } else {
        stepStates.upload = "error";
      }
      break;
    default:
      break;
  }

  return stepStates;
}

function renderProcessing(stage, message = "", recording = state.currentRecording) {
  const normalizedStage = PROCESS_SNAPSHOTS[stage] ? stage : "idle";
  const snapshot = PROCESS_SNAPSHOTS[normalizedStage];
  const resolvedMessage = message || snapshot.message;
  const stepStates = stepStateMap(normalizedStage, recording);

  state.processingStage = normalizedStage;
  elements.processTitle.textContent = snapshot.title;
  elements.processPill.textContent = snapshot.pill;
  elements.processPill.className = snapshot.pillClass;
  elements.processMessage.textContent = resolvedMessage;

  elements.progressSteps.forEach((step) => {
    step.dataset.state = stepStates[step.dataset.step] || "pending";
  });
}

function renderGlossary(entries) {
  elements.glossaryList.innerHTML = "";
  if (!entries.length) {
    elements.glossaryList.innerHTML = "<li>No glossary entries yet.</li>";
    return;
  }

  entries.forEach((entry) => {
    const item = document.createElement("li");
    item.innerHTML = `
      <strong>${escapeHtml(entry.navajo_term)}</strong>
      <p>${escapeHtml(entry.english_meaning)}</p>
      <p class="helper-copy">${escapeHtml(entry.notes || "No notes")}</p>
    `;
    elements.glossaryList.appendChild(item);
  });
}

function renderApprovedList(entries) {
  elements.approvedList.innerHTML = "";
  if (!entries.length) {
    elements.approvedList.innerHTML = "<li>No approved memory yet.</li>";
    return;
  }

  entries.forEach((entry) => {
    const item = document.createElement("li");
    item.innerHTML = `
      <strong>${escapeHtml((entry.corrected_transcript || "").slice(0, 70) || "Untitled phrase")}</strong>
      <p>${escapeHtml((entry.final_translation || "").slice(0, 140) || "No final translation yet.")}</p>
      <p class="helper-copy">${escapeHtml(entry.topic_tags || "No tags")}</p>
      <button class="button secondary" type="button" data-load-recording="${entry.id}">Open</button>
    `;
    elements.approvedList.appendChild(item);
  });
}

function displaySessionState(entry) {
  if (entry.status === "approved") {
    return "approved";
  }
  return entry.processing_stage || entry.status || "needs_review";
}

function renderRecentList(entries) {
  elements.recentList.innerHTML = "";
  if (!entries.length) {
    elements.recentList.innerHTML = "<li>No sessions yet.</li>";
    return;
  }

  entries.forEach((entry) => {
    const item = document.createElement("li");
    item.innerHTML = `
      <strong>${escapeHtml(entry.original_filename)}</strong>
      <p>${escapeHtml((entry.corrected_transcript || entry.raw_transcript || "Transcript pending").slice(0, 120))}</p>
      <p class="helper-copy">${escapeHtml(displaySessionState(entry))}</p>
      <button class="button secondary" type="button" data-load-recording="${entry.id}">Open</button>
    `;
    elements.recentList.appendChild(item);
  });
}

function renderHitList(target, hits, emptyText, formatter) {
  target.innerHTML = "";
  if (!hits.length) {
    target.innerHTML = `<li>${emptyText}</li>`;
    return;
  }

  hits.forEach((hit) => {
    const item = document.createElement("li");
    item.innerHTML = formatter(hit);
    target.appendChild(item);
  });
}

function renderRecording(recording) {
  state.currentRecording = recording;
  elements.reviewPanel.hidden = false;
  elements.audioPlayer.src = recording.audio_url;
  elements.rawTranscript.value = recording.raw_transcript || "";
  elements.correctedTranscript.value = recording.corrected_transcript || "";
  elements.translationText.value = recording.final_translation || recording.draft_translation || "";
  elements.translationNotes.value = recording.translation_notes || "";
  elements.topicTags.value = recording.topic_tags || "";
  elements.draftExplanation.textContent = recording.draft_explanation || "No explanation yet.";
  elements.confidenceBadge.textContent = `Confidence: ${recording.confidence || "low"}`;
  elements.statusBadge.textContent = recording.status === "approved" ? "Approved" : "Needs review";
  elements.statusBadge.className = `pill ${recording.status === "approved" ? "success" : "muted"}`;

  renderProcessing(
    recording.processing_stage || (recording.status === "approved" ? "approved" : "done"),
    recording.processing_message || "",
    recording
  );

  renderHitList(
    elements.glossaryHits,
    recording.glossary_hits || [],
    "No glossary hits for this transcript yet.",
    (hit) => `<strong>${escapeHtml(hit.navajo_term)}</strong> <span>${escapeHtml(hit.english_meaning)}</span>`
  );
  renderHitList(
    elements.exampleHits,
    recording.example_hits || [],
    "No similar approved phrases found yet.",
    (hit) => `
      <strong>${escapeHtml((hit.corrected_transcript || "").slice(0, 80))}</strong>
      <p>${escapeHtml((hit.final_translation || "").slice(0, 120) || "No approved translation")}</p>
      <p class="helper-copy">Similarity ${hit.score}</p>
    `
  );
  renderHitList(
    elements.warnings,
    recording.warnings || [],
    "No warnings.",
    (warning) => escapeHtml(warning)
  );
}

async function loadBootstrap() {
  const payload = await fetchJson("/api/bootstrap");
  state.bootstrap = payload;
  renderGlossary(payload.glossary || []);
  renderApprovedList(payload.approved_examples || []);
  renderRecentList(payload.recent_recordings || []);

  if (!state.currentRecording) {
    renderProcessing("idle");
  }
}

async function loadRecording(recordingId) {
  const payload = await fetchJson(`/api/recordings/${recordingId}`);
  renderRecording(payload.recording);
  setMessage(elements.captureMessage, payload.recording.processing_message || "Session loaded.");
}

async function uploadAudio() {
  if (!state.selectedFile) {
    setMessage(elements.captureMessage, "Choose or record an audio file first.");
    return;
  }

  elements.uploadButton.disabled = true;
  renderProcessing("uploading", `Uploading ${state.selectedFile.name}...`, null);
  setMessage(elements.captureMessage, "Uploading audio...");

  const formData = new FormData();
  formData.append("file", state.selectedFile, state.selectedFile.name);

  try {
    const created = await fetchJson("/api/recordings", { method: "POST", body: formData });
    renderRecording(created.recording);

    renderProcessing(
      "transcribing",
      `Transcribing with ${state.bootstrap?.app?.transcription_model || "the current model"}...`,
      created.recording
    );
    setMessage(elements.captureMessage, "Transcribing audio...");
    const transcribed = await fetchJson(`/api/recordings/${created.recording.id}/transcribe`, { method: "POST" });
    renderRecording(transcribed.recording);

    if (!transcribed.recording.raw_transcript?.trim()) {
      setMessage(
        elements.captureMessage,
        transcribed.recording.processing_message || "Transcription finished with issues."
      );
      await loadBootstrap();
      return;
    }

    renderProcessing("translating", "Drafting English translation...", transcribed.recording);
    setMessage(elements.captureMessage, "Drafting translation...");
    const drafted = await fetchJson(`/api/recordings/${created.recording.id}/draft-translation`, {
      method: "POST",
    });
    renderRecording(drafted.recording);
    setMessage(
      elements.captureMessage,
      drafted.recording.processing_message || "Phonetic transcript ready for review."
    );
    await loadBootstrap();
  } catch (error) {
    renderProcessing("error", error.message, state.currentRecording);
    setMessage(elements.captureMessage, error.message);
  } finally {
    elements.uploadButton.disabled = false;
  }
}

async function refreshDraft() {
  if (!state.currentRecording) {
    return;
  }

  elements.refreshButton.disabled = true;
  renderProcessing("translating", "Refreshing the English draft...", state.currentRecording);
  setMessage(elements.draftMessage, "Refreshing AI draft...");
  try {
    const payload = await fetchJson(`/api/recordings/${state.currentRecording.id}/refresh-draft`, {
      method: "POST",
      body: JSON.stringify({
        corrected_transcript: elements.correctedTranscript.value,
      }),
    });
    renderRecording(payload.recording);
    setMessage(
      elements.draftMessage,
      payload.recording.processing_message || "Draft translation ready for review."
    );
    await loadBootstrap();
  } catch (error) {
    renderProcessing("error", error.message, state.currentRecording);
    setMessage(elements.draftMessage, error.message);
  } finally {
    elements.refreshButton.disabled = false;
  }
}

async function approveRecording() {
  if (!state.currentRecording) {
    return;
  }

  elements.approveButton.disabled = true;
  renderProcessing("done", "Saving approved version...", state.currentRecording);
  setMessage(elements.draftMessage, "Saving approved version...");
  try {
    const payload = await fetchJson(`/api/recordings/${state.currentRecording.id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        corrected_transcript: elements.correctedTranscript.value,
        final_translation: elements.translationText.value,
        translation_notes: elements.translationNotes.value,
        topic_tags: elements.topicTags.value,
      }),
    });
    renderRecording(payload.recording);
    await loadBootstrap();
    setMessage(
      elements.draftMessage,
      payload.recording.processing_message || "Approved and added to project memory."
    );
  } catch (error) {
    renderProcessing("error", error.message, state.currentRecording);
    setMessage(elements.draftMessage, error.message);
  } finally {
    elements.approveButton.disabled = false;
  }
}

async function addGlossaryEntry(event) {
  event.preventDefault();
  try {
    await fetchJson("/api/glossary", {
      method: "POST",
      body: JSON.stringify({
        navajo_term: elements.glossaryTerm.value,
        english_meaning: elements.glossaryMeaning.value,
        notes: elements.glossaryNotes.value,
      }),
    });
    elements.glossaryForm.reset();
    await loadBootstrap();
    setMessage(elements.captureMessage, "Glossary entry saved.");
  } catch (error) {
    setMessage(elements.captureMessage, error.message);
  }
}

async function toggleRecording() {
  if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
    state.mediaRecorder.stop();
    return;
  }

  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    setMessage(elements.captureMessage, "This browser does not support in-browser audio recording.");
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.recordedChunks = [];
    state.mediaRecorder = new MediaRecorder(stream);

    state.mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) {
        state.recordedChunks.push(event.data);
      }
    });

    state.mediaRecorder.addEventListener("stop", () => {
      const blob = new Blob(state.recordedChunks, { type: state.mediaRecorder.mimeType || "audio/webm" });
      const extension = blob.type.includes("ogg") ? "ogg" : blob.type.includes("mpeg") ? "mp3" : "webm";
      state.selectedFile = new File([blob], `recording.${extension}`, { type: blob.type });
      elements.recordingState.textContent = "Recorded";
      elements.recordToggle.textContent = "Record again";
      renderProcessing("idle", "Recording captured. Ready to upload and transcribe.", null);
      setMessage(elements.captureMessage, "Recording captured. Click Transcribe Audio to continue.");
      stream.getTracks().forEach((track) => track.stop());
    });

    state.mediaRecorder.start();
    elements.recordingState.textContent = "Listening";
    elements.recordToggle.textContent = "Stop listening";
    renderProcessing("listening", "Listening to the microphone...", null);
    setMessage(elements.captureMessage, "Recording in progress...");
  } catch (error) {
    renderProcessing("error", "Microphone access was denied.", null);
    setMessage(elements.captureMessage, "Microphone access was denied.");
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

elements.fileInput.addEventListener("change", (event) => {
  state.selectedFile = event.target.files?.[0] || null;
  if (state.selectedFile) {
    renderProcessing("idle", `Selected ${state.selectedFile.name}. Ready to upload.`, null);
    setMessage(elements.captureMessage, `Ready to transcribe ${state.selectedFile.name}.`);
  }
});

elements.uploadButton.addEventListener("click", uploadAudio);
elements.refreshButton.addEventListener("click", refreshDraft);
elements.approveButton.addEventListener("click", approveRecording);
elements.glossaryForm.addEventListener("submit", addGlossaryEntry);
elements.recordToggle.addEventListener("click", toggleRecording);

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-load-recording]");
  if (!button) {
    return;
  }
  loadRecording(button.dataset.loadRecording).catch((error) => {
    renderProcessing("error", error.message, state.currentRecording);
    setMessage(elements.captureMessage, error.message);
  });
});

loadBootstrap().catch((error) => {
  renderProcessing("error", error.message, state.currentRecording);
  setMessage(elements.captureMessage, error.message);
});
