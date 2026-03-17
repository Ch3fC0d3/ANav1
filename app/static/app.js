const state = {
  bootstrap: null,
  currentRecording: null,
  selectedFile: null,
  mediaRecorder: null,
  recordedChunks: [],
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
      <p class="helper-copy">${escapeHtml(entry.status)}</p>
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
  elements.statusBadge.className = `pill ${recording.status === "approved" ? "" : "muted"}`;

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
}

async function loadRecording(recordingId) {
  const payload = await fetchJson(`/api/recordings/${recordingId}`);
  renderRecording(payload.recording);
}

async function uploadAudio() {
  if (!state.selectedFile) {
    setMessage(elements.captureMessage, "Choose or record an audio file first.");
    return;
  }

  elements.uploadButton.disabled = true;
  setMessage(elements.captureMessage, "Uploading and transcribing...");
  const formData = new FormData();
  formData.append("file", state.selectedFile, state.selectedFile.name);

  try {
    const payload = await fetchJson("/api/recordings", { method: "POST", body: formData });
    renderRecording(payload.recording);
    setMessage(elements.captureMessage, "Phonetic transcript ready for review.");
    await loadBootstrap();
  } catch (error) {
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
  setMessage(elements.draftMessage, "Refreshing AI draft...");
  try {
    const payload = await fetchJson(`/api/recordings/${state.currentRecording.id}/refresh-draft`, {
      method: "POST",
      body: JSON.stringify({
        corrected_transcript: elements.correctedTranscript.value,
      }),
    });
    renderRecording(payload.recording);
    setMessage(elements.draftMessage, "Draft updated.");
  } catch (error) {
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
    setMessage(elements.draftMessage, "Approved and added to project memory.");
  } catch (error) {
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
      setMessage(elements.captureMessage, "Recording captured. Click Transcribe Audio to continue.");
      stream.getTracks().forEach((track) => track.stop());
    });

    state.mediaRecorder.start();
    elements.recordingState.textContent = "Recording";
    elements.recordToggle.textContent = "Stop recording";
    setMessage(elements.captureMessage, "Recording in progress...");
  } catch (error) {
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
    setMessage(elements.captureMessage, error.message);
  });
});

loadBootstrap().catch((error) => {
  setMessage(elements.captureMessage, error.message);
});
