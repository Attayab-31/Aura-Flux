/**
 * static/js/main.js
 * =============================================================================
 * Frontend Controller for AuraFLUX AI Video Studio
 * =============================================================================
 * Handles form validation, non-secret preference caching, real-time status polling,
 * interactive scene storyboard generation, and video playback.
 */

document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const form = document.getElementById("video-creation-form");
    const submitBtn = document.getElementById("submit-job-btn");
    const storyText = document.getElementById("story_text");
    const charCount = document.getElementById("char-count");
    const durationInput = document.getElementById("duration_minutes");
    const durationDisplay = document.getElementById("duration-display");
    const estimatedScenesLabel = document.getElementById("estimated-scenes-label");
    const imageCountRange = document.getElementById("image_count_range");
    const imageCountInput = document.getElementById("image_count");
    const imageCountValue = document.getElementById("image-count-value");
    const imageCountDetail = document.getElementById("image-count-detail");
    const recommendedImageCountButton = document.getElementById("recommended-image-count");
    const sampleBtn = document.getElementById("sample-btn");
    const styleSelect = document.getElementById("style_preset");
    const stylePreviewImage = document.getElementById("style-preview-image");
    const stylePreviewPlaceholder = document.getElementById("style-preview-placeholder");
    const stylePreviewTitle = document.getElementById("style-preview-title");
    const stylePreviewStatus = document.getElementById("style-preview-status");
    const voiceSelect = document.getElementById("tts_voice");
    const voicePreviewButton = document.getElementById("preview-voice-btn");
    const voicePreviewStatus = document.getElementById("voice-preview-status");
    const voicePreviewAudio = document.getElementById("voice-preview-audio");

    // Pipeline Monitor Elements
    const jobStatusBadge = document.getElementById("job-status-badge");
    const currentStageLabel = document.getElementById("current-stage-label");
    const progressPercentage = document.getElementById("progress-percentage");
    const progressBarFill = document.getElementById("progress-bar-fill");
    const stepDescriptionText = document.getElementById("step-description-text");
    const scenesGrid = document.getElementById("scenes-grid");
    const storyboardCountBadge = document.getElementById("storyboard-count-badge");
    const terminalContent = document.getElementById("terminal-content");
    const toggleTerminalBtn = document.getElementById("toggle-terminal-btn");
    const terminalChevron = document.getElementById("terminal-chevron");

    // Video Output Elements
    const videoPlayerCard = document.getElementById("video-player-card");
    const masterVideoPlayer = document.getElementById("master-video-player");
    const downloadVideoBtn = document.getElementById("download-video-btn");
    const videoStatsSummary = document.getElementById("video-stats-summary");

    // Persist only non-secret creative preferences.
    const STORAGE_KEYS = {
        STYLE: "aura_flux_style",
        VOICE: "aura_flux_voice",
        IMAGE_COUNT: "aura_flux_image_count",
        ACTIVE_JOB: "aura_flux_active_job_id"
    };
    // Remove credentials saved by older versions of this page immediately.
    localStorage.removeItem("aura_flux_deepgram_key");
    localStorage.removeItem("aura_flux_openai_key");

    let activeJobId = null;
    let pollInterval = null;
    let renderedLogCount = 0;
    let imageCountWasCustomized = false;

    // =========================================================================
    // 1. CREDENTIAL PERSISTENCE & INITIALIZATION
    // =========================================================================
    const loadCachedSettings = () => {
        const cachedStyle = localStorage.getItem(STORAGE_KEYS.STYLE);
        const cachedVoice = localStorage.getItem(STORAGE_KEYS.VOICE);
        const cachedImageCount = Number(localStorage.getItem(STORAGE_KEYS.IMAGE_COUNT));

        if (cachedStyle) document.getElementById("style_preset").value = cachedStyle;
        if (cachedVoice) document.getElementById("tts_voice").value = cachedVoice;
        if (cachedImageCount >= 2 && cachedImageCount <= 60) {
            imageCountRange.value = String(cachedImageCount);
            imageCountInput.value = String(cachedImageCount);
            imageCountWasCustomized = true;
        }
    };

    const saveSettingsToStorage = () => {
        localStorage.removeItem("aura_flux_deepgram_key");
        localStorage.removeItem("aura_flux_openai_key");
        localStorage.setItem(STORAGE_KEYS.STYLE, document.getElementById("style_preset").value);
        localStorage.setItem(STORAGE_KEYS.VOICE, document.getElementById("tts_voice").value);
        localStorage.setItem(STORAGE_KEYS.IMAGE_COUNT, imageCountInput.value);
    };

    loadCachedSettings();

    // All previews are bundled static files. These handlers make no generation API calls.
    const loadStylePreview = () => {
        const styleKey = styleSelect.value;
        const styleLabel = styleSelect.selectedOptions[0]?.textContent.trim() || "Selected style";
        stylePreviewImage.hidden = false;
        stylePreviewPlaceholder.hidden = true;
        stylePreviewTitle.textContent = styleLabel;
        stylePreviewImage.src = `/static/previews/styles/${encodeURIComponent(styleKey)}.svg`;
        stylePreviewImage.alt = `${styleLabel} example: a cottage beside a lake, rendered in this style.`;
        stylePreviewStatus.textContent = "Fixed example image. Selecting a style only loads its bundled file.";
    };
    stylePreviewImage.addEventListener("error", () => {
        stylePreviewImage.hidden = true;
        stylePreviewPlaceholder.hidden = false;
        stylePreviewPlaceholder.querySelector("span").textContent = "The bundled style illustration is unavailable.";
        stylePreviewStatus.textContent = "No image generation is started from this page.";
    });
    styleSelect.addEventListener("change", loadStylePreview);
    loadStylePreview();

    const playVoicePreview = () => {
        const voice = voiceSelect.value;
        voicePreviewAudio.src = `/static/previews/voices/${encodeURIComponent(voice)}.mp3`;
        voicePreviewAudio.hidden = false;
        voicePreviewAudio.load();
        voicePreviewStatus.textContent = "Bundled sample audio. No Deepgram request is made.";
        voicePreviewAudio.play().catch(() => {
            voicePreviewStatus.textContent = "Sample loaded. Use the audio player to listen.";
        });
    };
    voicePreviewButton.addEventListener("click", playVoicePreview);
    voicePreviewAudio.addEventListener("error", () => {
        voicePreviewStatus.textContent = "This bundled voice sample is unavailable.";
    });
    voiceSelect.addEventListener("change", () => {
        voicePreviewAudio.pause();
        voicePreviewAudio.removeAttribute("src");
        voicePreviewAudio.hidden = true;
        voicePreviewStatus.textContent = "Fixed sample audio. Playing it does not call Deepgram.";
    });

    // Check for existing active or recent completed job
    const checkRecentJobsOnLoad = async () => {
        try {
            const cachedJobId = localStorage.getItem(STORAGE_KEYS.ACTIVE_JOB);
            if (cachedJobId) {
                const resp = await fetch(`/api/status/${cachedJobId}`);
                if (resp.ok) {
                    const job = await resp.json();
                    activeJobId = cachedJobId;
                    await pollJobStatus();
                    if (job.status === "RUNNING" || job.status === "QUEUED") {
                        if (!pollInterval) pollInterval = setInterval(pollJobStatus, 1200);
                    }
                    return;
                }
            }

            // Fallback: Check most recent job from backend
            const jobsResp = await fetch("/api/jobs");
            if (jobsResp.ok) {
                const jobsData = await jobsResp.json();
                const jobsList = jobsData.jobs || [];
                if (jobsList.length > 0) {
                    const latest = jobsList[0];
                    activeJobId = latest.job_id;
                    localStorage.setItem(STORAGE_KEYS.ACTIVE_JOB, activeJobId);
                    await pollJobStatus();
                    if (latest.status === "RUNNING" || latest.status === "QUEUED") {
                        if (!pollInterval) pollInterval = setInterval(pollJobStatus, 1200);
                    }
                }
            }
        } catch (err) {
            console.debug("No previous job restored:", err);
        }
    };

    checkRecentJobsOnLoad();

    // Password visibility toggle
    document.querySelectorAll(".toggle-password").forEach(btn => {
        btn.addEventListener("click", () => {
            const input = btn.previousElementSibling;
            if (input.type === "password") {
                input.type = "text";
                btn.innerHTML = '<i class="fa-solid fa-eye-slash text-xs"></i>';
            } else {
                input.type = "password";
                btn.innerHTML = '<i class="fa-solid fa-eye text-xs"></i>';
            }
        });
    });

    // =========================================================================
    // 2. TIMING & TEXT METRIC CALCULATORS
    // =========================================================================
    const updateMetrics = () => {
        const durationMins = parseFloat(durationInput.value);
        const totalSeconds = Math.round(durationMins * 60);
        const estimatedScenes = Math.min(60, Math.max(2, Math.round(totalSeconds / 7)));

        if (!imageCountWasCustomized) {
            imageCountRange.value = String(estimatedScenes);
            imageCountInput.value = String(estimatedScenes);
        }
        updateImageCountSummary();

        durationDisplay.textContent = `${durationMins.toFixed(2)} mins (~${totalSeconds} sec)`;
        estimatedScenesLabel.textContent = `Suggested: ~${estimatedScenes} images`;

        const text = storyText.value.trim();
        const chars = text.length;
        const words = text ? text.split(/\s+/).length : 0;
        charCount.textContent = `${chars} characters • ${words} words`;
    };

    const updateImageCountSummary = () => {
        if (!imageCountInput || !imageCountRange) return;
        const imageCount = Number(imageCountInput.value);
        const totalSeconds = Math.round(parseFloat(durationInput.value) * 60);
        if (!Number.isFinite(imageCount) || imageCount < 2) return;
        imageCountValue.textContent = `${imageCount} ${imageCount === 1 ? "image" : "images"}`;
        const averageSeconds = totalSeconds / imageCount;
        imageCountDetail.textContent = `About ${averageSeconds.toFixed(1)} seconds per image. More images create faster cuts and take longer to generate.`;
        imageCountRange.setAttribute("aria-valuetext", `${imageCount} images, about ${averageSeconds.toFixed(1)} seconds each`);
    };

    imageCountRange.addEventListener("input", () => {
        imageCountWasCustomized = true;
        imageCountInput.value = imageCountRange.value;
        updateImageCountSummary();
    });
    imageCountInput.addEventListener("input", () => {
        imageCountWasCustomized = true;
        const count = Math.min(60, Math.max(2, Number(imageCountInput.value) || 2));
        imageCountRange.value = String(count);
        updateImageCountSummary();
    });
    imageCountInput.addEventListener("change", saveSettingsToStorage);
    imageCountRange.addEventListener("change", saveSettingsToStorage);
    imageCountInput.addEventListener("change", () => {
        imageCountInput.value = imageCountRange.value;
        updateImageCountSummary();
    });
    recommendedImageCountButton.addEventListener("click", () => {
        imageCountWasCustomized = false;
        updateMetrics();
        saveSettingsToStorage();
    });

    durationInput.addEventListener("input", updateMetrics);
    storyText.addEventListener("input", updateMetrics);
    updateMetrics();

    // Sample Story Inserter
    const SAMPLE_STORIES = [
        "In the year 2142, humanity's deepest research outpost on Mars breached the subterranean ice caverns. Beneath the frozen crust of Olympus Mons, ancient bioluminescent crystals pulsed with rhythmic sapphire light. As Commander Elena Vance took her first steps across the crystalline floor, atmospheric monitors detected a mysterious harmonic vibration echoing through the cavern walls.",
        "Rain slicked the neon-drenched pavements of Neo-Kyoto as Detective Ren pulled his trench coat tight against the cold wind. Towering holographic billboards cast electric violet and cyan reflections into the mist. Deep within the shadow of the Arasaka tower, an encrypted datapad had been abandoned, glowing faintly in the darkness.",
        "Deep in the mist-shrouded emerald valleys of the Scottish Highlands, an ancient stone fortress stood watch over the roaring waters of Loch Ness. As golden twilight spilled across the heather-covered ridges, a solitary eagle soared toward the snowcapped peaks, riding the silent winds of antiquity."
    ];

    let currentSampleIdx = 0;
    sampleBtn.addEventListener("click", () => {
        storyText.value = SAMPLE_STORIES[currentSampleIdx % SAMPLE_STORIES.length];
        currentSampleIdx++;
        updateMetrics();
        storyText.focus();
    });

    // Toggle Console
    toggleTerminalBtn.addEventListener("click", () => {
        terminalContent.classList.toggle("hidden");
        terminalChevron.classList.toggle("rotate-180");
    });

    // =========================================================================
    // 4. FORM SUBMISSION & JOB INITIATION
    // =========================================================================
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        saveSettingsToStorage();

        const story = storyText.value.trim();
        if (!story) {
            alert("Please provide a story script.");
            return;
        }

        submitBtn.disabled = true;
        submitBtn.innerHTML = `
            <i class="fa-solid fa-spinner fa-spin text-lg"></i>
            <span>Orchestrating Pipeline...</span>
        `;

        // Reset UI state
        renderedLogCount = 0;
        terminalContent.innerHTML = "";
        videoPlayerCard.classList.add("hidden");
        masterVideoPlayer.pause();
        masterVideoPlayer.src = "";
        jobStatusBadge.className = "px-2.5 py-1 rounded-full text-xs font-bold font-mono bg-brand-500/20 text-brand-300 border border-brand-500/30";
        jobStatusBadge.textContent = "QUEUED";

        // Reset Stage Checkmarks
        for (let s = 1; s <= 5; s++) {
            const el = document.getElementById(`stage-step-${s}`);
            if (el) {
                el.className = "flex items-center space-x-3 text-xs text-slate-500 py-1 transition";
                el.querySelector("i").className = "fa-solid fa-circle-check";
            }
        }

        const formData = new FormData(form);

        try {
            const resp = await fetch("/api/create_video", {
                method: "POST",
                body: formData,
                headers: { "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content }
            });

            if (!resp.ok) {
                const errData = await resp.json();
                throw new Error(errData.error || "Failed to start generation job");
            }

            const data = await resp.json();
            activeJobId = data.job_id;
            localStorage.setItem(STORAGE_KEYS.ACTIVE_JOB, activeJobId);

            // Start polling
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(pollJobStatus, 1200);
            pollJobStatus();

        } catch (err) {
            alert(`Error: ${err.message}`);
            submitBtn.disabled = false;
            submitBtn.innerHTML = `
                <i class="fa-solid fa-film text-lg"></i>
                <span>Create video</span>
            `;
        }
    });

    // =========================================================================
    // 5. REAL-TIME JOB POLLING & UI SYNCHRONIZATION
    // =========================================================================
    const pollJobStatus = async () => {
        if (!activeJobId) return;

        try {
            const resp = await fetch(`/api/status/${activeJobId}`);
            if (!resp.ok) return;

            const job = await resp.json();

            // 1. Update Progress Bar & Description
            const pct = Math.min(100, Math.max(0, job.progress_percent || 0));
            progressBarFill.style.width = `${pct}%`;
            progressPercentage.textContent = `${pct}%`;
            currentStageLabel.textContent = job.stage || "In Progress";
            stepDescriptionText.textContent = job.step_description || "";

            // 2. Update Status Badge
            if (job.status === "RUNNING") {
                jobStatusBadge.className = "px-2.5 py-1 rounded-full text-xs font-bold font-mono bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 animate-pulse";
                jobStatusBadge.textContent = "RUNNING";
            } else if (job.status === "COMPLETED") {
                jobStatusBadge.className = "px-2.5 py-1 rounded-full text-xs font-bold font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30";
                jobStatusBadge.textContent = "COMPLETED";
            } else if (job.status === "FAILED") {
                jobStatusBadge.className = "px-2.5 py-1 rounded-full text-xs font-bold font-mono bg-rose-500/20 text-rose-300 border border-rose-500/30";
                jobStatusBadge.textContent = "FAILED";
            }

            // 3. Update Pipeline Checklist Stage
            updatePipelineChecklist(job.progress_percent, job.status);

            // 4. Update Storyboard Scene Cards
            if (job.scenes && job.scenes.length > 0) {
                renderStoryboardScenes(job.scenes);
            }

            // 5. Append new console logs
            if (job.logs && job.logs.length > renderedLogCount) {
                const newLogs = job.logs.slice(renderedLogCount);
                newLogs.forEach(line => {
                    const lineDiv = document.createElement("div");
                    lineDiv.className = "log-line text-[11px] leading-tight text-slate-300";
                    lineDiv.textContent = line;
                    terminalContent.appendChild(lineDiv);
                });
                renderedLogCount = job.logs.length;
                terminalContent.scrollTop = terminalContent.scrollHeight;
            }

            // 6. Handle Job Completion
            if (job.status === "COMPLETED") {
                clearInterval(pollInterval);
                submitBtn.disabled = false;
                submitBtn.innerHTML = `
                    <i class="fa-solid fa-rotate-right text-lg"></i>
                    <span>Create another video</span>
                `;

                // Display Video Player Card
                if (job.video_url) {
                    videoPlayerCard.classList.remove("hidden");
                    masterVideoPlayer.src = job.video_url;
                    downloadVideoBtn.href = job.video_url;
                    downloadVideoBtn.setAttribute("download", `ai_video_${activeJobId.substring(0, 8)}.mp4`);
                    videoStatsSummary.textContent = `Duration: ${job.total_duration_seconds}s | Render Time: ${job.elapsed_seconds}s`;

                    const diskPathDisplay = document.getElementById("disk-path-display");
                    const directUrlDisplay = document.getElementById("direct-url-display");
                    if (diskPathDisplay) {
                        const filename = job.video_url.split("/").pop();
                        diskPathDisplay.textContent = `static\\exports\\${filename}`;
                        diskPathDisplay.title = `E:\\Transitional_video_automation\\static\\exports\\${filename}`;
                    }
                    if (directUrlDisplay) {
                        directUrlDisplay.href = job.video_url;
                        directUrlDisplay.textContent = `${window.location.origin}${job.video_url}`;
                    }

                    videoPlayerCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
                }
            }

            // 7. Handle Job Failure
            if (job.status === "FAILED") {
                clearInterval(pollInterval);
                submitBtn.disabled = false;
                submitBtn.innerHTML = `
                    <i class="fa-solid fa-triangle-exclamation text-lg"></i>
                    <span>Retry Generation</span>
                `;
                alert(`Video generation failed:\n${job.error || "Unknown error"}`);
            }

        } catch (err) {
            console.error("Polling error:", err);
        }
    };

    // =========================================================================
    // 6. PIPELINE CHECKLIST ACTIVE STATES
    // =========================================================================
    const updatePipelineChecklist = (pct, status) => {
        const stages = [
            { id: 1, min: 10, max: 20 },
            { id: 2, min: 20, max: 40 },
            { id: 3, min: 40, max: 70 },
            { id: 4, min: 70, max: 85 },
            { id: 5, min: 85, max: 100 }
        ];

        stages.forEach(s => {
            const el = document.getElementById(`stage-step-${s.id}`);
            const icon = el.querySelector("i");

            if (pct >= s.max || status === "COMPLETED") {
                el.className = "flex items-center space-x-3 text-xs stage-complete py-1";
                icon.className = "fa-solid fa-circle-check text-emerald-400";
            } else if (pct >= s.min && pct < s.max && status !== "FAILED") {
                el.className = "flex items-center space-x-3 text-xs stage-active py-1";
                icon.className = "fa-solid fa-spinner fa-spin text-cyan-400";
            } else {
                el.className = "flex items-center space-x-3 text-xs text-slate-500 py-1";
                icon.className = "fa-regular fa-circle text-slate-600";
            }
        });
    };

    // =========================================================================
    // 7. DYNAMIC STORYBOARD SCENES RENDERER
    // =========================================================================
    const renderStoryboardScenes = (scenes) => {
        storyboardCountBadge.textContent = `${scenes.length} Scenes`;

        // If count changed or first render, construct structure
        const existingCards = scenesGrid.querySelectorAll(".scene-card");
        if (existingCards.length !== scenes.length) {
            scenesGrid.innerHTML = "";
            scenes.forEach(sc => {
                const card = document.createElement("div");
                card.id = `scene-card-${sc.scene_id}`;
                card.className = "scene-card rounded-xl border border-slate-800 bg-surface-950/70 p-3 space-y-2 transition-all hover:border-slate-700";
                card.innerHTML = `
                    <div class="flex items-center justify-between">
                        <div class="flex items-center space-x-2">
                            <span class="font-bold text-xs text-white">Scene ${sc.scene_id}</span>
                            <span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                                ${sc.motion_type}
                            </span>
                            <span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400">
                                ${sc.transition_type}
                            </span>
                        </div>
                        <span class="scene-status-badge text-[10px] font-mono text-slate-500">Pending</span>
                    </div>

                    <div class="scene-preview-container rounded-lg overflow-hidden bg-slate-900 aspect-video flex items-center justify-center border border-slate-800/80">
                        <div class="text-[11px] text-slate-600 font-mono flex items-center space-x-1.5">
                            <i class="fa-solid fa-image"></i>
                            <span>Awaiting Frame...</span>
                        </div>
                    </div>

                    <p class="scene-narration text-xs text-slate-300 italic line-clamp-2">${sc.narration_text}</p>
                `;
                scenesGrid.appendChild(card);
            });
        }

        // Update card contents
        scenes.forEach(sc => {
            const card = document.getElementById(`scene-card-${sc.scene_id}`);
            if (!card) return;

            const badge = card.querySelector(".scene-status-badge");
            const previewContainer = card.querySelector(".scene-preview-container");

            if (sc.image_ready && sc.image_url) {
                badge.textContent = "Frame Ready";
                badge.className = "scene-status-badge text-[10px] font-mono text-emerald-400";

                // Ensure image is displayed
                if (!previewContainer.querySelector("img")) {
                    previewContainer.innerHTML = `
                        <img src="${sc.image_url}?t=${Date.now()}" alt="Scene ${sc.scene_id}" class="w-full h-full object-cover">
                    `;
                }
            } else if (sc.audio_ready) {
                badge.textContent = `Voiced (${sc.audio_duration ? sc.audio_duration.toFixed(1) : ""}s)`;
                badge.className = "scene-status-badge text-[10px] font-mono text-cyan-400";
            }
        });
    };
});
