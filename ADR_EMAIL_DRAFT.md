# Email to ADR / National Election Watch

**To:** nationalelectionwatch@gmail.com
**CC:** contact@adrindia.org
**Subject:** Bulk data access request — open-source transparency project (PolitiTrack)

---

Dear ADR team,

I'm writing because ADR's work and the myneta.info platform have been
foundational to a project I'm building, and I'd like to ask about a more
sustainable way to access your dataset.

**About the project**

I'm building **PolitiTrack** — an open-source web platform that makes
candidate affidavit data more discoverable for ordinary citizens. The goal
is plain-language summaries, side-by-side comparisons, term-over-term
wealth tracking, and constituency-level maps — building on the data ADR
has structured from the Election Commission's affidavit archive.

The project is non-commercial, fully open source, and exists in the same
public-interest spirit as myneta itself. It is intended to surface ADR's
work to a new audience, not replace it. Every politician profile on the
site links back to its original source URL on myneta, and ADR is credited
as the primary data source on every page and in the project's README.

I currently have working coverage of Punjab and Bihar (all assembly cycles
plus a subset of Lok Sabha data), and the architecture is ready to scale
to all states.

**The ask**

I have been scraping myneta carefully — single-threaded with a 2-second
rate limit, identified User-Agent, and aggressive on-disk caching to avoid
re-fetching pages. So far this has been workable for two states, but
expanding to all of India this way would mean tens of thousands of HTTP
requests over several days, which is not the right way to use your
servers.

Would ADR consider sharing a bulk export of the structured candidate-level
data — preferably as CSV or JSON dumps per state per cycle? Even a
one-time data snapshot would be enormously useful and would let me stop
hammering myneta entirely.

I'm happy to:

- Credit ADR prominently on every page (already doing this)
- Open-source the project under a permissive licence
- Share the parsed and enriched dataset back to ADR if useful
- Sign any data-use agreement you require
- Discuss the scope and methodology over a call

**About me**

I'm an independent developer (no commercial interest in this project). The
codebase lives at <github.com/yourusername/politrack> and the
methodology page is at <yoursite.in/methodology>. Happy to share more
detail or a live demo.

Thanks for the work you do — the country is better for it.

Best regards,
Gurneet Bedi
[your phone number]
[your city]
