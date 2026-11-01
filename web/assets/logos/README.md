# Third-party source marks

These files are the **official marks** of the scholarly sources ScholarGuide can
search. They are used only to identify the source a result came from; the
copyright and trademark rights stay with their respective owners, and their
inclusion here is not an endorsement.

| File | Owner | Where it was obtained |
|---|---|---|
| `arxiv-logo.svg` | arXiv (Cornell University) | `https://static.arxiv.org/static/design-system/latest/logos/arxiv-logo.svg` |
| `openreview-logo.png` | OpenReview | `https://github.com/openreview/openreview-web` → `public/images/openreview_logo_512.png` |
| `openalex-lockup.png` | OpenAlex | `https://openalex.org/brand-assets/openalex-lockup.png` |
| `s2-logo.svg` | Semantic Scholar (Allen Institute for AI) | the official homepage wordmark at `https://www.semanticscholar.org/` |
| `dblp-logo.png` | DBLP (Schloss Dagstuhl) | the official `https://dblp.org/favicon.ico` (64 px frame) |
| `acl-logo.svg` | ACL Anthology (ACL) | `https://aclanthology.org/images/acl-logo.svg` |
| `lune-logo-text.svg` | Lune Research | `https://luneresearch.com/lune-logo-text.svg` |

Rules for this directory:

- Keep the files byte-identical to the official asset; do not recolour, crop or
  redraw them.
- Serve them from the local copy only (the app exposes `/assets/logos/…`); never
  hotlink a third-party host at runtime.
- The landing page shows them on a neutral plate so the official colours stay
  legible in both the light and the dark theme.
- A mark identifies a source. It is never evidence about a paper, an acceptance
  decision or a publication record — that still comes from the source's own
  records.
