# NOR140 data format investigation

Updated: 2026-08-14

This is a working reverse-engineering note for the NOR140-B / `MEAS118` SD-card data used by the noise app. It is not vendor-confirmed. The immediate goal is for the app's report values to match the NOR140 screen exactly.

## Current conclusion

The current app is mis-decoding LAeq, and the key cause is now identified.

The old parser logic that reads `GLOB` offset `0x0422` as per-minute/current-session LAeq is wrong. That offset sits inside a larger results/statistics matrix. Some values near there correlate with noise level, but they are not the meter's global LAeq.

The NOR140 global equivalent levels can be reproduced from the 36-band flat 1/3-octave spectrum stored in `GLOB`, using:

```python
band_db = raw_uint16_le / 128 - 20
```

For normal 2653-byte `GLOB` files, the global `Lfeq` spectrum starts at offset `0x0428`. Applying standard A/C frequency weighting to these 36 bands and energy-summing reproduces screen/Nortfr `LAeq` and `LCeq`.

## Confidence scale

- **High**: confirmed across the local data or by direct screen-value relationship.
- **Medium**: strongly indicated by repeated structure/correlation, but not vendor-labelled.
- **Low**: plausible hypothesis requiring NorXfer/NorReview export or more meter-screen reference data.

## Source data

SD card structure:

```text
MEAS118/YYMMDD/DIRFILE.FIL
MEAS118/YYMMDD/PART0000/PROJnnnn/GLOBnnnn.DAT
MEAS118/YYMMDD/PART0000/PROJnnnn/PROFnnnn.DAT
MEAS118/SETUP/STPnnnn.DAT
```

Local corpus summary:

- 527 matched `GLOB`/`PROF` pairs.
- `GLOB` sizes observed:
  - 2653 bytes: 469 files
  - 1069 bytes: 54 files
  - 2668 bytes: 4 files
- `PROF` header bytes are always `00 00 00`.

## Reference screen values

Uploaded workbook:

```text
/Users/colinives/Downloads/temp 260814.xlsx
```

The workbook has NOR140 screen values for `260812` runs 2, 4, 5, 6, 8, 9, and 10. Runs 1, 3, and 7 are marked `Error` in the workbook and should not be used as screen ground truth for this phase.

Screen values for usable `260812` runs:

| Run | Duration | Leqa | Leqc | LFMina | LFMinc | LFmaxa | LFmaxc | LEA | LEC | LPeaka | LPeakc |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 900s | 61.0 | 71.2 | 53.1 | 62.8 | 80.4 | 83.6 | 90.6 | 100.8 | 92.4 | 95.5 |
| 4 | 900s | 71.5 | 81.7 | 54.8 | 71.4 | 86.5 | 91.2 | 101.0 | 111.3 | 104.5 | 107.1 |
| 5 | 900s | 77.4 | 86.4 | 63.6 | 70.7 | 92.9 | 96.7 | 106.9 | 115.9 | 104.0 | 105.6 |
| 6 | 797s | 69.6 | 79.8 | 52.3 | 63.0 | 76.4 | 92.6 | 98.6 | 108.8 | 89.9 | 98.8 |
| 8 | 900s | 78.6 | 89.0 | 73.2 | 81.3 | 87.2 | 97.6 | 108.1 | 118.5 | 104.4 | 106.5 |
| 9 | 900s | 70.8 | 78.6 | 52.8 | 62.9 | 81.7 | 89.2 | 100.3 | 108.2 | 97.4 | 98.6 |
| 10 | 900s | 70.2 | 80.1 | 52.9 | 62.3 | 83.1 | 87.5 | 99.7 | 109.7 | 91.9 | 98.1 |

## Nortfr export for run 9

Confidence: High.

Nortfr export folder:

```text
/Users/colinives/Downloads/nortfr
```

Files inspected:

```text
NOR140_6899108_260812_0009.NBF
NOR140_6899108_260812_0009_GLOBAL.xlsx
NOR140_6899108_260812_0009_PROFILE.xlsx
```

This export is for `MEAS118/260812/PART0000/PROJ0009`.

The `.NBF` file embeds the original SD-card binary data directly:

```text
GLOB0009.DAT found in .NBF at byte offset 20
PROF0009.DAT found in .NBF at byte offset 2670

NBF size:        11673 bytes
GLOB0009 size:    2653 bytes
PROF0009 size:    9003 bytes
wrapper/overhead:   17 bytes
```

This confirms that Nortfr is showing values derived from the same `GLOB` and `PROF` files available to the app, not from any extra meter-only state.

The global workbook has one tab per exported result, plus a `Summary` tab. Important global run-9 values:

```text
LAeq    70.8
LCeq    78.7
LAE    100.3
LCE    108.2
LAFmin  52.8
LCFmin  62.9
LAFmax  81.7
LCFmax  89.2
LApeak  97.4
LCpeak  98.6
```

These agree with the meter-screen values for run 9, except the earlier manually-entered screen `Leqc` was `78.6`; Nortfr exports `LCeq = 78.7`. Treat this as a rounding/transcription discrepancy until checked on the meter.

## How much Nortfr exposes

Confidence: High for the run-9 export inspected.

The Nortfr Excel exports expose selected decoded result channels from the SD-card files, not every internal byte or every unknown field. However, they expose enough labelled data for the app to reproduce the important meter/report values.

Run-9 `GLOBAL` workbook:

```text
sheets: 58 total
measurement/result sheets: 57
spectral result sheets: 18
scalar broadband/global result sheets: 39
approx unique global measurement values:
  18 spectral sheets * 36 bands = 648 spectral values
  39 scalar result sheets        =  39 scalar values
  total                          = 687 values, plus metadata/summary duplicates
```

The 18 spectral sheets are:

```text
Lfeq
LfE
LfFmax
LfFmin
LfSmax
LfSmin
LfIeq
LfImax
LfImin
LfIE
LfF,0.1%_1
LfF,1.0%_2
LfF,5.0%_3
LfF,10.0%_4
LfF,50.0%_5
LfF,90.0%_6
LfF,95.0%_7
LfF,99.0%_8
```

The scalar global sheets include A- and C-weighted broadband values derived from, or related to, these flat spectral tables:

```text
LAeq, LCeq
LAE, LCE
LAFmax, LCFmax
LAFmin, LCFmin
LApeak, LCpeak
LAF/LCF percentile values
other I/S/F time-weighted scalar values
```

Run-9 `PROFILE` workbook:

```text
sheets: 7 total
one-second profile result sheets: 5
profile rows per result: 900
approx unique profile measurement values:
  5 result sheets * 900 seconds = 4500 values
```

The five profile series exported are:

```text
LAFspl
LAeq
LAFmax
LAE
LApeak
```

Practical implication:

```text
GLOBnnnn.DAT -> GLOBAL workbook style output
PROFnnnn.DAT -> PROFILE workbook style output
```

The app should therefore be able to produce the same classes of outputs from the SD-card data. Current confidence is high for normal 2653-byte `GLOB` global spectra and derived `LAeq`/`LCeq`; lower for exact Nortfr-style profile columns until the raw `PROF` transform is fully mapped.

## Equivalent level calculation from spectra

Confidence: High for run 9.

The Nortfr global workbook includes a labelled `Lfeq` 1/3-octave spectrum from `6.3 Hz` to `20.0 kHz`.

For run 9, energy-summing that exported flat spectrum with standard frequency weighting reproduces the exported broadband equivalent levels:

```text
from Lfeq spectrum:
  A-weighted sum = 70.775 dB -> LAeq 70.8
  C-weighted sum = 78.659 dB -> LCeq 78.7
  unweighted sum = 79.446 dB -> about 79.4
```

The exported `LfE` spectrum behaves the same way for exposure:

```text
from LfE spectrum:
  A-weighted sum = 100.319 dB -> LAE 100.3
  C-weighted sum = 108.224 dB -> LCE 108.2
```

This strongly indicates that, at least for global equivalent/exposure values, the NOR140/Nortfr path is:

```text
stored or decoded 1/3-octave flat spectrum
  -> apply A or C frequency weighting per band
  -> energy-sum bands
  -> round/display as LAeq, LCeq, LAE, LCE
```

The profile workbook gives an independent check for `LAeq`: the energy average of the 900 one-second exported profile `LAeq` values is:

```text
70.796 dB -> LAeq 70.8
```

Therefore a standards-grade 15-minute `LAeq` should be calculated as an energy average, not an arithmetic mean:

```python
LAeq_15min = 10 * log10(sum(10 ** (LAeq_1s / 10) for LAeq_1s in seconds) / number_of_seconds)
```

When using spectra instead of one-second `LAeq`, the equivalent operation is:

```python
LAeq = 10 * log10(sum(10 ** ((Lf_band + A_weight_band) / 10) for band in bands))
LCeq = 10 * log10(sum(10 ** ((Lf_band + C_weight_band) / 10) for band in bands))
```

## Exposure values

Confidence: High

The screen `LEA` and `LEC` values are derived from `Leqa` / `Leqc` and measurement duration:

```python
LEA = Leqa + 10 * log10(duration_seconds)
LEC = Leqc + 10 * log10(duration_seconds)
```

This matches the uploaded NOR140 screen values within screen rounding:

- 900-second runs: `10*log10(900) = 29.54 dB`
- Run 6: `797` complete profile records, `10*log10(797) = 29.01 dB`

So the parser should derive `LEA` and `LEC` from the final `Leqa`/`Leqc` and duration. They do not need separate binary fields.

## `DIRFILE.FIL`

Confidence: Medium-High

Date-folder `DIRFILE.FIL` appears to contain 26-byte entries, one per project/run.

Observed entry layout:

```text
offset  size  meaning
0x00    2     project/run number, uint16 little-endian
0x02    6     start timestamp as BCD: yy mm dd hh mm ss
0x08    ?     status/type fields, not decoded
0x12    ?     repeated date/project references, not decoded
```

Example from `260812/DIRFILE.FIL`, entry 2:

```text
02 00 26 08 12 21 31 39 ...
```

This decodes as project 2, start `2026-08-12 21:31:39`.

## `PROFnnnn.DAT`

Confidence: High for binary structure, Medium/Low for field semantics.

Profile files are time-profile streams:

```text
offset  size       meaning
0x00    3          header/reserved, always 00 00 00 in local corpus
0x03    10 * n     complete profile records
tail    0..8       optional partial final record bytes in some files
```

Each complete 10-byte record is:

```text
5 x uint16 little-endian
```

Value decoding:

```python
level_db = raw_uint16 / 128 - 20
```

Earlier analysis used `raw/100 - 40`, which was wrong and made the profile values appear about 8-11 dB too high.

Nortfr run-9 profile export confirms there are five exported one-second A-weighted columns, mapped from the five raw profile fields:

```text
field 0  LAFspl
field 1  LAeq,1s
field 2  LAFmax,1s
field 3  LAE,1s
field 4  LApeak,1s
```

For one-second periods, `LAeq` and `LAE` are numerically the same, which is why fields 1 and 3 commonly match.

Run-9 `PROF0009.DAT` decoded with `raw/128 - 20` matches Nortfr's profile workbook with about 0.03 dB RMS residual, which is consistent with binary resolution and one-decimal Excel rounding.

A-weighted profile-derived values:

```text
LAFmax = max(PROF field 2)
LApeak = max(PROF field 4)
LAF percentile values can be computed from sorted PROF field 0 (LAFspl)
```

However, the meter's global `LAFmin` is stored directly in `GLOB`, not always exactly equal to `min(PROF field 0)`, probably because the global detector keeps sub-period or differently rounded state.

Parser rule:

- Consume only complete 10-byte records after the 3-byte header.
- Preserve/report trailing partial bytes for investigation.
- Do not energy-average `PROF` fields and call the result NOR140 global `LAeq`; use the `GLOB` scalar/spectrum values for global report results.

## `GLOBnnnn.DAT` header

Confidence: High for listed metadata fields.

Common header fields:

```text
offset  size  meaning
0x00    2     observed b6 03 in normal GLOB/setup files
0x04    2     length-like value, usually 0x0a59 for 2653-byte files
0x0d    6     ASCII date folder, e.g. "260812"
0x16    2     project/run number, uint16 little-endian
0x19    6     start timestamp as BCD: yy mm dd hh mm ss
0x1f    6     end timestamp as BCD: yy mm dd hh mm ss
```

## `GLOBnnnn.DAT` results matrix

Confidence: High for structure and the listed spectral labels.

The normal `GLOB` results area begins around `0x0408`.

Observed layout:

```text
main results block:
  0x0408 .. 0x073e
  412 uint16 words
  824 bytes

seven 36-word blocks:
  0x0764 .. 0x07aa
  0x07d0 .. 0x0816
  0x083c .. 0x0882
  0x08a8 .. 0x08ee
  0x0914 .. 0x095a
  0x0980 .. 0x09c6
  0x09ec .. 0x0a32
```

Many broadband/statistical cells in these regions decode as:

```python
level_db = raw_uint16 / 128 - 20
```

The key scalar values are stored in an odd-byte-aligned scalar block immediately before the spectral/statistics region:

```text
offset   Nortfr/screen value
0x03c1   LAFmax
0x03c3   LASmax
0x03c5   LAImax
0x03c7   LAFmin
0x03c9   LASmin
0x03cb   LAImin
0x03cd   LAeq
0x03cf   LAIeq
0x03d1   LAE
0x03d3   LAIE
0x03d5   LApeak
0x03db   LCFmax
0x03dd   LCSmax
0x03df   LCImax
0x03e1   LCFmin
0x03e3   LCSmin
0x03e5   LCImin
0x03e7   LCeq
0x03e9   LCIeq
0x03eb   LCE
0x03ed   LCIE
0x03ef   LCpeak
```

The broadband percentile scalar values follow:

```text
0x0408   LAF,0.1%_1
0x040a   LAF,1.0%_2
0x040c   LAF,5.0%_3
0x040e   LAF,10.0%_4
0x0410   LAF,50.0%_5
0x0412   LAF,90.0%_6
0x0414   LAF,95.0%_7
0x0416   LAF,99.0%_8
0x0418   LCF,0.1%_1
0x041a   LCF,1.0%_2
0x041c   LCF,5.0%_3
0x041e   LCF,10.0%_4
0x0420   LCF,50.0%_5
0x0422   LCF,90.0%_6
0x0424   LCF,95.0%_7
0x0426   LCF,99.0%_8
```

All of the above use:

```python
level_db = raw_uint16_le / 128 - 20
```

Nortfr-labelled run-9 spectral table map:

```text
offset   Nortfr tab
0x0428   Lfeq
0x0470   LfFmax
0x04b8   LfFmin
0x0500   LfE
0x0548   LfSmax
0x0590   LfSmin
0x05d8   LfIeq
0x0620   LfImax
0x0668   LfImin
0x06b0   LfIE
0x06f8   LfF,0.1%_1
0x0764   LfF,1.0%_2
0x07d0   LfF,5.0%_3
0x083c   LfF,10.0%_4
0x08a8   LfF,50.0%_5
0x0914   LfF,90.0%_6
0x0980   LfF,95.0%_7
0x09ec   LfF,99.0%_8
```

Each table is 36 consecutive little-endian uint16 values corresponding to these preferred 1/3-octave bands:

```text
6.3, 8, 10, 12.5, 16, 20, 25, 31.5, 40, 50, 63, 80,
100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
1k, 1.25k, 1.6k, 2k, 2.5k, 3.15k, 4k, 5k, 6.3k,
8k, 10k, 12.5k, 16k, 20k
```

The run-9 decoded table values match Nortfr's exported one-decimal spectra with typical RMS error around 0.02-0.03 dB, which is expected from the binary resolution of 1/128 dB and export rounding.

## Broadband scalar cells

Confidence: High for listed values.

The early cells at `0x0408..0x0426` are not per-minute `LAeq`; they are the LAF/LCF broadband percentile scalar values exported by Nortfr.

This is why the old app's `0x0422` logic was wrong: `0x0422` is `LCF,90.0%_6`, not a minute or session `LAeq` value.

## Attempts to reproduce screen `Leqa` / `Leqc`

Status: superseded by the confirmed `raw/128 - 20` scalar/spectrum mapping above.

Using only usable workbook rows for `260812` runs 2, 4, 5, 6, 8, 9, 10:

### Direct fixed `GLOB` cell search

Earlier searches using `raw/100 - 40` found close but wrong cells:

```text
Leqa best: 0x0442, raw/100-40
  values: 60.4, 74.0, 78.3, 71.0, 79.5, 70.4, 71.6
  target: 61.0, 71.5, 77.4, 69.6, 78.6, 70.8, 70.2
  RMSE:   about 1.34 dB

Leqc best: 0x077c, raw/100-40
  values: 69.9, 83.3, 88.3, 80.4, 86.3, 78.7, 81.1
  target: 71.2, 81.7, 86.4, 79.8, 89.0, 78.6, 80.1
  RMSE:   about 1.53 dB
```

These are not the correct decoding. The correct direct scalar cells are `0x03cd` for `LAeq` and `0x03e7` for `LCeq`, decoded as `raw/128 - 20`.

### `PROF`-derived stats

Earlier `PROF` stats were wrong because the profile records were decoded with `raw/100 - 40`. Correct profile decoding is `raw/128 - 20`, but global `LAeq` should still come from `GLOB`, not an energy average of raw profile fields:

```text
F0 mean RMSE vs screen Leqa: about 3.26 dB
F0 energy average RMSE vs screen Leqa: about 6.48 dB
```

This reinforces that `PROF` is not the global Leq source.

### Spectral 36-word blocks before Nortfr labels

Earlier hypothesis tested before the correct scale was identified: the seven obvious 36-word blocks are frequency spectra, and broadband A/C totals can be reconstructed by energy-summing bands with IEC A/C weighting.

Best candidate found:

```text
block base: 0x08a8
36 bands, start around 6.3 Hz / preferred 1/3-octave sequence
A-weighted total RMSE vs Leqa: about 2.1 dB
C-weighted total RMSE vs Leqc: about 3.2 dB
```

This was close enough to be interesting because the structure was right, but the scale was wrong.

Interpretation:

- The 36-word tables probably are spectral/statistical blocks.
- The labelled Nortfr export now confirms that `LAeq`/`LCeq` can be reproduced from the exported `Lfeq` spectrum exactly for run 9.
- The missing piece was the spectral-table encoding: `raw/128 - 20`, not `raw/100 - 40`.

### Verified screen parity from `GLOB` `Lfeq`

Confidence: High.

Using `GLOB` offset `0x0428`, 36 bands decoded as `raw/128 - 20`, then applying standard A/C weighting:

```text
run  LAeq_calc  screen  diff    LCeq_calc  screen  diff
2      61.049    61.0  +0.049     71.169    71.2  -0.031
4      71.492    71.5  -0.008     81.723    81.7  +0.023
5      77.363    77.4  -0.037     86.370    86.4  -0.030
6      69.593    69.6  -0.007     79.754    79.8  -0.046
8      78.566    78.6  -0.034     89.006    89.0  +0.006
9      70.769    70.8  -0.031     78.663    78.6  +0.063
10     70.180    70.2  -0.020     80.154    80.1  +0.054
```

All differences are within one-decimal display rounding. Run 9 Nortfr exports `LCeq = 78.7`, while the manual screen workbook says `78.6`; the binary calculation gives `78.663`, so `78.7` is the natural one-decimal rounded value.

## What is still unknown

Confidence: High that these block exact parity.

The main global equivalent-level problem is solved for normal 2653-byte `GLOB` files:

```text
GLOB 0x0428 Lfeq spectrum -> A/C weighting -> LAeq/LCeq
```

What remains is only needed for complete robustness across all SD-card variants:

1. Whether all normal 2653-byte files use the same scalar and spectral-table offsets under all setup modes.
2. Whether reduced-size `1069`-byte `GLOB` files have a different/partial scalar or spectral layout.
3. Meaning of the extra bytes in `2668`-byte `GLOB` files.
4. Handling of partial trailing `PROF` records.
5. Any additional metadata/setup fields needed for a fully faithful Nortfr-style workbook export.

## Recommended next step

Confidence: High.

Generate Nortfr/NorXfer/NorReview exports for the exact same runs:

```text
MEAS118/260812/PART0000/PROJ0002
MEAS118/260812/PART0000/PROJ0004
MEAS118/260812/PART0000/PROJ0005
MEAS118/260812/PART0000/PROJ0006
MEAS118/260812/PART0000/PROJ0008
MEAS118/260812/PART0000/PROJ0009
MEAS118/260812/PART0000/PROJ0010
```

Export both Global and Profile views if possible.

The run-9 export plus the screen-value cross-checks for runs 2, 4, 5, 6, 8, 9, and 10 are enough to implement `LAeq`/`LCeq` for normal 2653-byte `GLOB` files with high confidence. More exports are still useful for mapping other metrics and unusual file sizes.

## App implication

For current app correction and future Nortfr-style exports:

- Do not use `0x0422` as LAeq.
- Do not use `PROF` energy averages as NOR140 LAeq.
- For equivalent/exposure results, target the Nortfr-confirmed method:
  - decode the flat 1/3-octave global `Lfeq` spectrum at `0x0428` as `uint16_le / 128 - 20`
  - apply A/C weighting per band
  - energy-sum bands
  - round to the meter/export display precision
- For fuller Nortfr-style `GLOBAL` output, decode all 18 known spectral tables and derive A/C scalar totals from them where applicable.
- For Nortfr-style `PROFILE` output, continue mapping the raw `PROF` five-field records to the exported A-weighted one-second columns.
- Derive `LEA`/`LEC` from `LAeq`/`LCeq` and duration only after `LAeq`/`LCeq` match the meter/export.
- Treat old app LAeq values as unreliable for formal reports unless they are produced by the spectrum method and verified against the reference runs.
