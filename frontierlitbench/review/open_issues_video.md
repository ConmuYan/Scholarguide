# Track A open issues

1. **18 papers excluded from scoring** (protocol §4.4): first-author institution unverifiable via OpenAlex (rate-limited during re-verification; caches retained for resume). Affected titles listed in `corpus/gold_video.jsonl` with `institution_status=unverified_excluded`.
2. **A-dimension evidence level**: 53 of the 94 re-verified papers use author-level evidence (OpenAlex `last_known_institutions`) instead of work-level authorships; flagged per-paper in gold records. May over/under-state affiliation at publication time.
3. **Policy confirmations pending group sign-off** (see review_workbook.html header): citation source policy (OpenAlex single source), none_available=0.0, industry-lab quartile mapping population (labs observed in Track A pool).
4. FB-13 task filter corrected to CVPR + ICCV per FB-1.0: 57 scored papers (46 + 11). This corrects task eligibility only; the corpus and its labels are unchanged.
