# Validation brief — run end times, and the 8-byte PROF record

**Scope:** `7dd0181`, `b7e5ad3`, `2467772`, `18af8f8` and the review-response
commit that follows them on `master`.
**Author:** Catherine Ives-Yim, 2026-08-17. **Revision 2**, after external review.
**Purpose:** give a reviewer everything needed to falsify the claims below
without taking any of them on trust.

The work started as a display change — show each run's end time as well as its
start. It turned up a latent parser defect, a peer-sync data loss, and two of my
own claims that were wrong. All are covered here, and the retractions are left
visible rather than tidied away, because how they were caught is the most useful
thing in this document.

**Revision 2 changes:** five findings from review are resolved (§7). The
arithmetic fallback has been **removed entirely**, which changes the behaviour
described in C4. Corpus counts in §3 were wrong and are corrected. C7 is now
verified by measurement rather than by grep.

---

## 0. How to reproduce the environment

Flask is not installed system-wide on the dev machine, so `test_modules.py`
needs a venv:

```bash
python3 -m venv /tmp/nm-venv && /tmp/nm-venv/bin/pip install -q flask && /tmp/nm-venv/bin/python3 test_modules.py
```

Expected: `All 192 checks passed.` The suite needs `MEAS118/` extracted in the
repo root (527 GLOB/PROF pairs, ~3 MB zipped, not committed — see `.gitignore`).

---

## 1. Claims, ranked by how much damage a wrong one would do

| # | Claim | Confidence | Where it could still be wrong |
| --- | --- | --- | --- |
| C1 | The 1069-byte GLOB variant has 4-channel, 8-byte PROF records | **High** | ch0/ch1 ordering — now corroborated across all 54, see §2 |
| C2 | `0x22` is the measurement end time | **High** | Nortfr has no end-time row to check against; corroborated indirectly |
| C3 | `prof_record_size()` classifies correctly | High | Two signals now, and it fails closed rather than guessing |
| C4 | A wrong end time is never displayed | **High** | True by construction since the fallback was removed |
| C5 | Two files have an *unset* end time | Medium | "Unset" is inference; could be an aborted run or a clock fault |
| C6 | The backfill was purely additive | High | Directly observable — see §4 |
| C7 | End times display correctly at all six sites | High | Modal measured at 360 px; other five sites still grep + API only |

Weakest link is now **C5**, and it is inconsequential — see §3.

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

**ch0/ch1 — resolved on review.** Revision 1 called this the weakest point in
the brief: ch0 and ch1 have energy averages equal to two decimals, so I had
separated them only by analogy with the 10-byte layout, and warned that no check
in the suite would catch a swap. The review closed it with a test I had missed —
comparing ch0 against the **GLOB LAF percentile scalars**, which are an
independent discriminator that does not rely on the energy average:

* ch0 matches the LAF percentiles better than ch1 in **all 54** files
* ch1's energy average matches GLOB LAeq in 52 of 54, mean error ~0.003 dB
* ch2 max reproduces LAFmax exactly in all 54
* ch3 max reproduces LApeak exactly in all 54

`test_modules.py` now asserts all four across every 8-byte file rather than the
single example it used before, including the percentile discriminator — the only
one that separates ch0 from ch1, and so the only thing standing between a
channel swap and silence.

**What would still falsify it.** A 1069-byte file whose 8-byte decode does not
reproduce its GLOB scalars. A Nortfr export of any 2025-08-23/24/31 run remains
the one piece of external evidence we lack, though it now matters much less.

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
end 23:42:36.

**External corroboration.** Revision 1 flagged as an open gap that no Nortfr
export had been checked for an end-time row. The review checked: **Nortfr does
not expose one.** But its final profile timestamps agree with the decoded
boundary, or with the final one-second period immediately before it, and the
corpus contains a correctly decoded cross-midnight run. So C2 is corroborated
indirectly and the gap is closed as far as it can be.

**Was it worth storing rather than deriving?** Partly, and my commit message for
`7dd0181` overstated it. The corrected distribution:

| Case | Files |
| --- | --- |
| elapsed = record count | 385 |
| elapsed = count − 1 | 137 |
| zero-record aborted run (1 s elapsed) | 3 |
| unset / zeroed | 2 |

Revision 1 of this table said 387 / 135 / 2. It was counted before the
record-size fix and ignored the zero-record runs entirely — a reviewer recount
gave the figures above, which are what the current code produces.

So after the C1 fix the value is always the record count or one less. That is
*nearly* derivable — you would have to guess which, per run, with nothing in the
file to say. Storing it removes the guess, and reading it is what exposed C1.
But "not derivable" as originally written was too strong.

**C4 — the guard, and a retraction.** `read_end_time()` cross-checks implied
elapsed against the duration at `0x03bd`, rejecting beyond 300 s:

```bash
/tmp/nm-venv/bin/python3 -c "
import glob
from nor140_format import read_end_time
f=sorted(glob.glob('MEAS118/**/GLOB*.DAT',recursive=True))
bad=[g for g in f if read_end_time(open(g,'rb').read()) is None]
print(len(bad),'of',len(f)); [print(' ',g) for g in bad]"
```

Expected: exactly 2 — `230830/PROJ0001` and `250711/PROJ0011`.

Revision 1 claimed this "never shows a wrong figure". **That was false**, and
the review was right to call it out. A run legitimately paused for more than
300 s would have been rejected, and the arithmetic fallback would then have
displayed a confident, wrong time — the fallback cannot model a pause either.

The fix is not a better threshold, because no threshold is safe in principle: a
pause is unbounded. **The arithmetic fallback has been removed entirely.** The
display now shows the meter's value or nothing. A rejected or absent end time
renders blank, which for evidence going to a council is strictly better than a
plausible-looking invention. `run_end_time()` is deleted, not merely unused.

That makes the original claim true by construction, and it makes the 300 s
threshold a fail-closed rule — when in doubt, show nothing — rather than a
silent substitution.

**C5 — are those two really unset?** Both read exactly `00:00:00`. I did not
blacklist that value, because a run can legitimately end at midnight. The
inference that they are unset rests on their being wildly inconsistent with
their own durations; an aborted run or a clock reset fits equally well. With the
fallback gone this is now inconsequential — either way the field is blank — so
it is a wording issue in the docs, not a behavioural one.

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

## 5. Peer sync — the highest-severity finding, found on review

`_run_to_dict()` exported the value under the key `end`, while
`import_sessions()` read `end_time`. Every peer-synced run therefore stored
`NULL` and fell back to arithmetic. Neither Pi had yet synced a run carrying an
end time, so no live data was lost — but it would have been on the next sync.

The fix exports **both** keys, deliberately:

* `end` — for display.
* `end_time` — the meter's own value, and the only field sync reads.

They are separate because exporting only `end` would have been the wrong fix:
before the fallback was removed, `end` could carry a *derived* value, and
syncing that would have written an arithmetic estimate into the peer's
`end_time` column, indistinguishable from meter data. Silently degrading real
metadata to a guess is worse than the NULL it replaced.

The regression test uses run 1 — 83 records but an 82 s duration — so any
arithmetic reconstruction lands at 12:09:53, one second past the meter's
12:09:52. Confirm the test actually bites by deleting the `'end_time'` line from
`_run_to_dict` and re-running: expect `FAIL meter end time survives peer sync`.

---

## 6. C7 — the display

Revision 1 admitted this was verified by grep and API rather than by loading a
page. The review found a real bug there: at 360 px the modal's wrapped title
pushed a `flex-shrink:0` action group past the modal edge, hiding the close
button.

Now measured rather than asserted. Chrome clamps headless windows to 500 px on
macOS, so the modal is rendered inside a 360 px iframe, which gets its own
viewport for media-query purposes, and the geometry is read back:

| | actions right | box right | close visible |
| --- | --- | --- | --- |
| before | 348 | 344 | **false** |
| after | 329 | 344 | true |

**Still not visually checked:** the other five sites — run list, session CSV,
assessment run rows, report run picker, generated report table. They are simpler
layouts, but that is an argument, not a measurement.

---

## 7. Review findings and disposition

| # | Finding | Disposition |
| --- | --- | --- |
| 1 | Peer sync discards the meter end time | Fixed; regression test added and verified to fail without the fix (§5) |
| 2 | 300 s guard can silently substitute a wrong estimate | Fixed by removing the fallback outright; claim retracted (§3) |
| 3 | Mobile run modal overflows at 360 px | Fixed; measured before/after (§6) |
| 4 | `prof_record_size()` could misclassify a future paused 8-byte run | Fixed in two passes — the first left an early return that defeated the conflict check; see below |
| 5 | Corpus counts inaccurate | Corrected here and in `NOR140_handoff.md` (§3) |

On finding 4, the classifier takes two signals: the duration, and the GLOB
variant. **Both are now derived before either is returned.** The first attempt
at this fix did not do that — the duration branch returned early, so a file
whose two signals disagreed silently took the duration's answer, and
`prof_record_size(7160, 716, 1069)` gave 10 despite the variant identifying
8-byte records. The fail-closed claim in this brief was therefore true of the
intent and not of the code. Caught on a second review pass and fixed.

The resolution order is now: disagreement → `None`; otherwise whichever signal
resolves; if neither, `None`. The parser skips a `None`, because guessing
misaligns every record in the file.

```bash
/tmp/nm-venv/bin/python3 -c "
from nor140_format import prof_record_size
print('signals conflict           :', prof_record_size(7160, 716, 1069))
print('paused 8-byte, variant known:', prof_record_size(7160, 3000, 1069))
print('nothing can decide         :', prof_record_size(7160, 3000, None))
print('normal 10-byte             :', prof_record_size(9000, 900, 2653))"
```

Expected `None`, `8`, `None`, `10`. All five branches are asserted in the suite,
along with the whole corpus classifying 473 ten-byte / 54 eight-byte with none
left unresolved — an unresolved file is skipped on import, so a regression there
would silently drop runs rather than fail loudly.

---

## 8. What the retracted claims should tell you

Two claims in revision 1 were wrong, and they failed in the same way.

The first: 54 files were documented as *logging at a 1.25 s period*. 1.25 is
10/8 — the ratio was an artefact of reading 8-byte records as 10-byte. I caught
that one myself, by asking what else produces exactly 5/4.

The second: the guard "never shows a wrong figure". I had reasoned about the
threshold and not about the fallback behind it, so I checked the part I had
built and missed the part I had inherited. The review caught it.

A third, from the follow-up pass: this brief described `prof_record_size()` as
failing closed on conflicting signals when the code returned early and never
compared them. That one is the most instructive of the three, because the
document and the code were written together and still disagreed — describing
intended behaviour is not evidence of it.

All three were confident statements resting on a single unexamined assumption.
Treat every "consistent across N files" claim here the same way, and ask what
*else* would produce that consistency. The candidate that stood out on this pass — ch0/ch1 resting
on analogy alone — is now pinned by an independent discriminator in the suite,
so the next one will have to be found somewhere else.
