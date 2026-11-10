"""Step 2a: Track A (Video) candidate discovery via Lune search_papers_many.

One call per frozen sub-direction (FB-1.0 Table 1). All calls filtered to the
Track A venue whitelist and the 2024-2026 window. Raw responses archived under
corpus/raw/ for reproducibility; merged/deduped in step 3.
"""
import json, os, time, datetime
from lune_client import LuneClient

VENUES = ["CVPR", "ICCV", "ICLR", "ICML", "NeurIPS", "ACM MM", "SIGGRAPH"]
YEAR_MIN, YEAR_MAX = 2024, 2026
LIMIT = 50

# Query angles: every angle maps to exactly one frozen sub-direction.
ANGLES = {
    "t2v_i2v": [  # 1. text/image-to-video
        "text-to-video generation with large-scale diffusion or transformer models",
        "image-to-video generation: animating a still image with motion and dynamics",
        "story-level long video generation with multiple coherent scenes and shots",
        "efficient and lightweight video generation: distillation, caching and acceleration methods",
        "unified architectures that generate both images and videos with one model",
        "autoregressive and token-based next-token-prediction models for video generation",
    ],
    "video_diffusion_dit": [  # 2. video diffusion & DiT
        "video diffusion models: architectures and training strategies for high fidelity video synthesis",
        "diffusion transformers (DiT) for scaling video generation models",
        "latent-space diffusion and VAE compression for efficient video generation",
        "flow matching and consistency models applied to video generation",
        "classifier-free guidance, sampling schedules and training recipes for video diffusion",
        "video score distillation and optimization-based video synthesis",
    ],
    "controllable_editing": [  # 3. controllable editing
        "instruction-based and text-driven editing of existing videos",
        "pose-, sketch-, depth- or segmentation-conditioned controllable video generation",
        "reference-image guided video editing of subjects, objects and characters",
        "video inpainting, object insertion and removal editing in generated or real video",
        "audio-, speech- and portrait-driven talking video generation and lip sync",
        "camera-motion-controlled and 3D-aware video generation and editing",
    ],
    "temporal_consistency": [  # 4. temporal consistency
        "temporal consistency and flicker reduction in diffusion-generated video",
        "long video generation preserving identity, appearance and object persistence across frames",
        "frame interpolation and video super-resolution for consistent and sharp video output",
        "evaluation benchmarks and metrics for temporal coherence of video generation models",
        "attention and feature propagation mechanisms for cross-frame consistency in video models",
        "autoregressive long video extension: extending short clips while maintaining consistency",
    ],
}

os.makedirs("corpus/raw", exist_ok=True)
c = LuneClient()
meta = {"retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "venues": VENUES, "year_min": YEAR_MIN, "year_max": YEAR_MAX,
        "calls": []}

for subdir, queries in ANGLES.items():
    print(f"== {subdir}: {len(queries)} angles ==")
    r = c.call("search_papers_many", queries=queries, limit=LIMIT, detail=True,
               year_min=YEAR_MIN, year_max=YEAR_MAX, venues=VENUES)
    fn = f"corpus/raw/many_{subdir}.json"
    json.dump(r, open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = len(r.get("results", []))
    meta["calls"].append({"subdirection": subdir, "n_queries": len(queries),
                          "n_results": n, "raw_file": fn})
    years = {}
    confs = {}
    for p in r.get("results", []):
        years[p.get("year")] = years.get(p.get("year"), 0) + 1
        confs[p.get("conference")] = confs.get(p.get("conference"), 0) + 1
    print(f"   {n} results | years={dict(sorted(years.items()))} | venues={confs}")
    time.sleep(2)

json.dump(meta, open("corpus/raw/collection_meta_2a.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("done")
