# estateplan

A single-file Python tool that keeps the *decisions* in an estate plan (people,
children, guardians, executors, agents, gifts, end-of-life choices) in a SQLite
database and regenerates the documents from it:

- Last Will & Testament
- Durable Power of Attorney
- Advance Health Care Directive
- HIPAA Authorization (release of medical information)

The document text follows the Massachusetts documents produced by Trust & Will
in 2020 and 2026, checked sentence by sentence against those PDFs. Fonts and
layout are not reproduced; the words are.

**This is not legal advice and the author is not a lawyer.** It exists so a
family can re-print their own documents with a corrected name or a reordered
list of backups without re-subscribing to a service. Have a lawyer review
anything you intend to sign.

## Running

Needs [uv](https://docs.astral.sh/uv/). Dependencies (Flask, reportlab) are
declared inline and installed on first run.

```
uv run estateplan.py                     # web editor at http://127.0.0.1:5077
uv run estateplan.py build               # write output/<date>/<Name>/<Doc>.pdf and .txt
uv run estateplan.py text jane will      # plain text of one document
uv run estateplan.py export --out family.local.json
uv run estateplan.py import family.local.json
uv run estateplan.py reseed              # wipe the database and reload
uv run estateplan.py verify ORIGINALS/   # diff generated text against reference PDFs
```

On first run the database is seeded from `family.local.json` if that file
exists next to the script, otherwise from a fictional Doe family built into the
script. `*.local.json`, the database, and all output are gitignored, so real
names never need to touch the repository.

## Data model

- **person**: name, address (one line per printed line), phone, email, birth date.
- **principal**: one per person who signs documents. Holds the spouse, state,
  notary jurisdiction, POA statute line, residence line, remains and ceremony
  choices, care preference, organ donation flag, and special instructions.
- **role**: ordered lists per principal. `child`, `guardian`, `executor`,
  `health_agent`, `hipaa_recipient`, `poa_agent`, `poa_backup`. First entry is
  primary, the rest are backups in order. The executor is also the digital
  executor, as in the source documents.
- **gift**: specific gifts (recipient + item) per principal.

## Verification

`verify` renders every document, extracts text with poppler's `pdftotext`, and
diffs it against the reference at sentence granularity after normalizing quotes,
whitespace, underscores, page headers and footers. The reference for a document
is `ORIGINALS/<Name>/<Doc>.txt` if present (for image-only PDFs, put an OCR
transcript there), otherwise `ORIGINALS/<Name>/<Doc>.pdf`.

## Deliberate deviations from the Trust & Will text

- **Will, Final Arrangements.** The 2026 Trust & Will will dropped this section
  even when the account still recorded a choice. It is rendered whenever
  remains or ceremony is set.
- **Directive, Final Arrangements.** Trust & Will's text cites a separate
  "Final Arrangement Wishes" document that the service never produces. The
  paragraph points at the will's Final Arrangements section instead.
- **POA, agents.** Trust & Will leaves agent names as blank lines to be
  handwritten. They are printed from the database (blank lines if none).
- **POA, instructions page and logos.** Omitted.
- **Directive, "prolong life" care preference.** Only the "receive care only
  if it will improve my condition" wording comes from Trust & Will. The
  opposite choice uses plain wording written for this tool.

## Known ambiguity in the POA text

If the spouse is also the agent (the usual case), two clauses under "Additional
Specific Powers (a) Make Gifts" overlap: gifts *to my spouse* are allowed up to
the annual federal gift-tax exclusion, while gifts *to my agent* are capped at
the lesser of $5,000 or 5% of the assets per year. The text does not say which
applies when they are the same person. The conservative reading is the smaller
cap. The wording is left as Trust & Will wrote it.

## At signing

- Will and POA: two witnesses who are not related to you and are not named in
  the documents, plus a notary. The will's notary block is a self-proving
  affidavit.
- Directive: two witnesses with the same restrictions; the notary block is
  optional. There is an initial line on the "Powers of Health Care Agent" page.
- Handwritten at signing: county on each notary block, dates of birth on the
  directive and HIPAA signature pages.

## License

MIT. See `LICENSE`.
