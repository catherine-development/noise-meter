# Validation brief — run end times, and the 8-byte PROF record

**Scope:** commits `7dd0181`, `b7e5ad3`, `2467772` on `master`.
**Author:** Catherine Ives-Yim, 2026-08-17.
**Purpose:** give a reviewer everything needed to falsify the claims below
without taking any of them on trust.

The work started as a display change — show each run's end time as well as its
start. It turned up a latent parser defect, and one of my own claims was wrong
and had to be retracted. Both are covered here, and the retracted claim is left
visible rather than tidied away, because the way it was caught is the most
useful thing in this document.

---

## 0. How to reproduce the environment

Flask is not installed system-wide on the dev machine, so `test_modules.py`
needs a venv:

```bash
python3 -m venv /tmp/nm-venv && /tmp/nm-venv/bin/pip install -q flask && /tmp/nm-venv/bin/python3 test_modules.py
```

Expected: `All 174 checks passed.` The suite needs `MEAS118/` extracted in the
repo root (527 GLOB/PROF pairs, ~3 MB zipped, not committed — see `.gitignore`).

---

## 1. Claims, ranked by how much damage a wrong one would do

| # | Claim | Confidence | Where it could still be wrong |
| --- | --- | --- | --- |
| C1 | The 1069-byte GLOB variant has 4-channel, 8-byte PROF records | High | Channel *identity* rests on matching GLOB scalars, not on a Nortfr export |
| C2 | `0x22` is the measurement end time | High | No Nortfr reference has been checked for an end-time row — see §5 |
| C3 | `prof_record_size()` picks the right size for all 527 files | High | The tie-break is empirical, not documented by Norsonic |
| C4 | The 300 s end-time guard separates good from unset | Medium | Threshold from one archive; a genuine outlier would be silently rejected |
| C5 | Two files have an *unset* end time | Medium | "Unset" is inference; could be an aborted run or a clock fault |
| C6 | The backfill was purely additive | High | Directly observable — see §4 |
| C7 | End times display at all six sites | High | Verified by grep + API, **not** by loading a page in a browser |

Weakest links first: **C5**, **C4**, then the untested part of **C2** in §5.

---

## 2. C1 — the 8-byte PROF record (the significant finding)

**Claim.** 54 of 527 files pair a 1069-byte GLOB with an 8-byte, 4-channel PROF
record. Read as 10-byte records — as the parser did until `2467772` — every one
decodes from misaligned data and yields a record count 0.8× the truth.

| | ch0 | ch1 | ch2 | ch3 | ch4 |
| --- | --- | --- | --- | --- | --- |
| 10-byte | LAFSPL | LAeq | LAFmax | LAE | LApeak |
| 8-byte | LAFSPL | LAeq | LAFmax | LApeak | — |

**Three independent supports.** Each is checkable on its own:

```bash
/tmp/nm-venv/bin/python3 - <<'PY'
import glob, os, struct, math, collections
from nor140_format import read_glob_scalars, read_duration_s, prof_record_size

# (a) divisibility
agg = collections.defaultdict(collections.Counter)
for g in glob.glob('MEAS118/**/GLOB*.DAT', recursive=True):
    p = g.replace('GLOB', 'PROF')
    if not os.path.exists(p): continue
    gsz = len(open(g, 'rb').read()); pay = os.path.getsize(p) - 3
    agg[gsz]['n'] += 1
    for rs in (8, 10): agg[gsz][rs] += (pay % rs == 0)
for k in sorted(agg): print('(a)', k, dict(agg[k]))

# (b) at 8 bytes the record count equals the elapsed time
b = 'MEAS118/250823/PART0000/PROJ0002/'
gd = open(b + 'GLOB0002.DAT', 'rb').read()
pay = os.path.getsize(b + 'PROF0002.DAT') - 3
print('(b) payload', pay, 'n@8', pay // 8, 'n@10', pay // 10,
      'duration', read_duration_s(gd))

# (c) decoded channels reproduce the GLOB scalars
d = open(b + 'PROF0002.DAT', 'rb').read()[3:]
ch = [[struct.unpack_from('<H', d, i*8 + c*2)[0]/128.0 - 20
       for i in range(len(d)//8)] for c in range(4)]
m = read_glob_scalars(gd, digits=2)
print('(c) GLOB  LAeq', m['laeq'], 'LAFmax', m['lafmax'], 'LApeak', m['lapeak'])
for i, c in enumerate(ch):
    print('    ch%d max %.2f  Leq %.2f'
          % (i, max(c), 10*math.log10(sum(10**(v/10) for v in c)/len(c))))
PY
```

Expected: (a) all 54 of the 1069-byte group divide by 8, only 8 of them by 10;
(b) `n@8` = 895 = elapsed, duration 894; (c) ch1 Leq = 80.28 = GLOB LAeq, ch2
max = 90.12 = GLOB LAFmax, ch3 max = 102.83 = GLOB LApeak.

**What would falsify it.** A 1069-byte file whose 8-byte decode does *not*
reproduce its GLOB scalars. Or a Nortfr export of any 1069-byte run — we have
none, which is the real gap. **If you can get one reference export for a
2025-08-23/24/31 run, that settles C1 outright and is the single most valuable
thing you could add.**

**Honest limit — the weakest point in this whole brief.** Channel *order* is
inferred from scalar agreement. LAFmax and LApeak are pinned hard: exact to the
hundredth, and they are distinguishable maxima. **ch0 vs ch1 is not.** Both have
an energy average of ~80.28, and I separated them only by analogy with the
10-byte layout.

That analogy has a wrinkle the reviewer should chase. In the Nortfr-verified
10-byte run 9, ch0 max (80.49) is *below* ch1 max (81.02). In the 8-byte file,
ch0 max (88.88) is *above* ch1 max (85.05) — the relationship inverts. That may
be nothing more than different signal content, but it is the one place where the
8-byte layout does not behave like the 10-byte one, and it is precisely the pair
I could not pin independently. If ch0 and ch1 are in fact swapped, the stored
LAFSPL and LAeq series would be exchanged for all 54 files, and **no check in
the suite would catch it** — the energy averages are equal to two decimals, so
the LAeq assertion in `test_modules.py` passes either way.

**Blast radius.** No 1069-byte run has ever been in a live database. Both Pis
hold 11 runs, all 2026-08-12/13, all 2653-byte. So the defect was latent in the
import path, no stored result was wrong, and no backfill was needed. Verify:

```bash
ssh flightdata@192.168.1.116 'sqlite3 ~/noise-meter/noise.db "SELECT DISTINCT s.date FROM runs r JOIN sessions s ON s.id=r.session_id;"'
```

---

## 3. C2/C4/C5 — the end-time field

**Claim.** `0x22` holds the end time as three BCD bytes. The header carries two
adjacent `YY MM DD hh mm ss` blocks, start at `0x19` and end at `0x1f`.

```bash
/tmp/nm-venv/bin/python3 -c "
from nor140_format import bcd
d=open('MEAS118/260812/PART0000/PROJ0009/GLOB0009.DAT','rb').read()
print(' '.join('%02d'%bcd(b) for b in d[0x18:0x25]))"
```

Expected `01 26 08 12 23 27 36 26 08 12 23 42 36` — start 2026-08-12 23:27:36,
end 23:42:36. Run 9 is one of the seven Nortfr-verified runs and is a 900 s run
starting at 23:27:36, so `23:42:36` is independently corroborated.

**Was it worth storing rather than deriving?** Partly. My commit message for
`7dd0181` overstates this, and the corrected table is:

| Case | Files |
| --- | --- |
| elapsed = record count | 387 |
| elapsed = count − 1 | 135 |
| unset | 2 |

So after the C1 fix it is always the record count or one less. That is *nearly*
derivable — you would just have to guess which, per run, with nothing in the file
to say. Storing it removes the guess, and reading it is what exposed C1. But
"not derivable" as originally written was too strong.

**C4 — the guard.** `read_end_time()` cross-checks implied elapsed against the
duration at `0x03bd`, rejecting beyond 300 s.

```bash
/tmp/nm-venv/bin/python3 -c "
import glob
from nor140_format import read_end_time
f=sorted(glob.glob('MEAS118/**/GLOB*.DAT',recursive=True))
bad=[g for g in f if read_end_time(open(g,'rb').read()) is None]
print(len(bad),'of',len(f)); [print(' ',g) for g in bad]"
```

Expected: exactly 2 — `230830/PROJ0001` and `250711/PROJ0011`.

**Where 300 came from.** Every other file agrees with its duration within 23 s;
the two rejects are out by 9,554 s and 46,474 s. So the gap is enormous and the
threshold is anywhere in it. **But it is one archive.** A genuinely paused run
that idled more than 300 s would be silently rejected and fall back to
arithmetic. It degrades softly and never shows a wrong figure — but it degrades
*silently*, which is a fair criticism. If you think it should log instead, say
so; I would not argue.

**C5 — are those two really unset?** Both read exactly `00:00:00`. I did not
blacklist that value, because a run can legitimately end at midnight. The
inference that they are unset rests on their being wildly inconsistent with
their own durations. An alternative reading — an aborted run, or a meter clock
reset — fits the evidence equally well. It does not change the handling either
way, but the *word* "unset" in the docs is an inference, not an observation.

---

## 4. C6 — the backfill

`backfill_glob.py` selected only rows missing spectral data, so on both Pis it
reported "nothing to do" and would never have filled `end_time` for the 11 runs
that already had spectra. The same gap silently applied to `duration_s` and
`full_scale`. Fixed in `b7e5ad3`.

Purely additive, on two independent signals: every LAeq printed unchanged
(`25.42→25.42`, `70.79→70.79`, …), and `Session aggregates recomputed: 0
changed`. Backups are at `~/noise-meter/noise.db.bak-endtime` on both Pis.

```bash
ssh flightdata@192.168.1.116 'cd ~/noise-meter && sqlite3 noise.db "SELECT COUNT(*), SUM(end_time IS NULL) FROM runs;"'
```

Expected `11|0`. Run against the `.bak-endtime` copy to confirm nothing but the
three header columns moved.

---

## 5. The gap I most want a second opinion on

**No Nortfr export has been checked for an end-time row.** All seven reference
pairs were compared cell-for-cell and pass, but I never asked whether the
exporter emits a stop time that would validate `0x22` directly. If Nortfr does
emit one, that is a direct check on C2 and it is sitting unused in files we
already have. I should have looked before writing the docs. Worth ten minutes.

Second: **C7 was verified structurally, not visually.** I confirmed the markup
is present in the deployed templates and that the API returns an end time for
every run, but I did not load a page. A reviewer with a browser should check the
run list, the run modal, the assessment run rows, the report run picker and a
generated report — particularly that `start–end` does not overflow its container
on narrow screens, which no check here covers.

---

## 6. What the retracted claim should tell you

I originally documented those 54 files as *logging at a 1.25 s period*, and
committed that to `NOR140_handoff.md`. It was wrong. 1.25 is 10/8 — the ratio was
an artefact of reading 8-byte records as 10-byte, not a property of the meter.

I caught it only when writing this document, by asking what else could produce
exactly 5/4. The lesson worth carrying into the review: a clean, tight,
repeatable number across many files reads as strong evidence, and it was — but
of the wrong proposition. Treat every "consistent across N files" claim above as
open to the same failure, and ask what *else* would produce that consistency.

Prime suspect by that standard is C1's ch0/ch1 ordering, which is the one place
I am relying on analogy rather than an exact match.
