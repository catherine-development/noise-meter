# NOR140 data format investigation

Updated: 2026-08-15

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

### `.NBF` wrapper

Confidence: Medium for run-9 wrapper layout.

Run-9 `.NBF` starts with a 20-byte wrapper/header, then embeds the original measurement data:

```text
0x00  2 bytes   0x0014, probably offset/length of wrapper header
0x02  8 bytes   ASCII "Nor-140\0"
0x0a 10 bytes   unknown wrapper fields
0x14           GLOB0009.DAT begins
0xa6e          PROF0009.DAT begins
```

The `PROF` start appears three bytes before `0x14 + len(GLOB)` because the end of `GLOB` and the three-byte zero `PROF` header overlap/abut in the package. For Excel replacement, generating `.NBF` is not necessary; the app can decode `GLOB` and `PROF` directly.

To reproduce Nortfr `.NBF` files exactly, more `.NBF` examples are needed to identify the 10 unknown wrapper bytes. This does not block reproducing Nortfr's GLOBAL/PROFILE Excel data.

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

The app should therefore be able to produce the same classes of outputs from the SD-card data. Current confidence is high for normal 2653-byte `GLOB` global scalars/spectra and Nortfr-style `PROF` profile columns.

## Nortfr workbook export structure

Confidence: High for run-9 GLOBAL/PROFILE exports.

Nortfr writes one sheet per measurement channel plus `Summary` and `Setup`.

### `Setup` sheet

Both GLOBAL and PROFILE setup sheets have the same rows:

```text
1  blank
2  File version
3  Filename
4  Report type
5  Bandwidth
6  Frequency range
7  Period length
8  Total number of periods
9  Number of periods before trigger
10 Number of periods after trigger
11 Trig time
12 Measurement effective duration
13 Full scale
14 Sensitivity
```

Known sources/inference:

```text
File version:
  Nortfr software/export version, e.g. "v1.0/6.1.1.51".
  Not an SD-card measurement value. Can be replaced by the app's own export version.

Filename:
  Nortfr-generated name: NOR140_<serial>_<YYMMDD>_<run>.NBF (...).
  The serial number "6899108" was not found in GLOB, PROF, DIRFILE, or STP files.
  The generated Excel workbook does contain an internal Excel absolute-path hint:
    C:\Users\...\Norsonic\Downloaded Measurements\NOR140_6899108\260812\
  This supports the idea that Nortfr knew the serial from its downloaded-measurements/device context,
  not from the raw SD-card GLOB/PROF payload. Treat serial as external/user-configured unless
  another meter-side file proves otherwise.

App requirement:
  Add an instrument serial number field to the add-record/import flow.
  Store it as app/session metadata, not decoded NOR140 measurement data.
  Default the field to the last serial number used to reduce repeated typing.
  Use it for Nortfr-style generated filenames and export metadata, e.g.:
    NOR140_<serial>_<YYMMDD>_<run>_GLOBAL.xlsx
    NOR140_<serial>_<YYMMDD>_<run>_PROFILE.xlsx

Report type:
  GLOBAL or PROFILE, chosen by export mode.

Bandwidth:
  "1/3 Octave", inferred from the 36-band spectral layout.

Frequency range:
  "6.3 Hz - 20.0 kHz", inferred from the 36 preferred 1/3-octave bands.

Period length:
  GLOBAL: effective duration as one period, e.g. 0:15:0.0.
  PROFILE: one second, e.g. 0:0:1.0.

Total number of periods:
  GLOBAL: 1.
  PROFILE: number of complete PROF records.

Number of periods before trigger:
  observed 0.

Number of periods after trigger:
  GLOBAL: 1.
  PROFILE: number of complete PROF records.

Trig time:
  GLOB start timestamp at 0x19, also in DIRFILE.

Measurement effective duration:
  normally number of complete PROF records in seconds.
  For normal 15-minute runs this is 900 seconds.

Full scale:
  stored in GLOB/STP at offset 0x4a as an unsigned integer dB value.
  Run 9 has 0x0082 = 130 dB.

Sensitivity:
  exported as -26.9 dB for run 9.
  Not yet confidently mapped. Common encodings and stable-offset searches did not identify a calibration field.
  Several candidate byte patterns in GLOB are ordinary measurement values, not stable setup fields.
```

### GLOBAL `Summary` sheet

Rows:

```text
row 1: group/scalar headers
row 2: blank
row 3: frequency headers under spectral groups
row 4: the single GLOBAL result row
rows 5-6: blank
rows 7-9: NC / NR / RC II rows
```

Columns:

```text
1  Period:
2  Time:
3  Duration:
4  blank
5  blank
6..43 scalar global values
44..79   Lfeq spectrum, 36 bands
80..115  LfFmax spectrum, 36 bands
116..151 LfFmin spectrum, 36 bands
152..187 LfE spectrum, 36 bands
188..223 LfSmax spectrum, 36 bands
```

The GLOBAL Summary scalar columns are:

```text
LAFmax, LASmax, LAImax, LAFmin, LASmin, LAImin,
LAeq, LAIeq, LAE, LAIE, LApeak,
LCFmax, LCSmax, LCImax, LCFmin, LCSmin, LCImin,
LCeq, LCIeq, LCE, LCIE, LCpeak,
LAF,0.1%_1, LAF,1.0%_2, LAF,5.0%_3, LAF,10.0%_4,
LAF,50.0%_5, LAF,90.0%_6, LAF,95.0%_7, LAF,99.0%_8,
LCF,0.1%_1, LCF,1.0%_2, LCF,5.0%_3, LCF,10.0%_4,
LCF,50.0%_5, LCF,90.0%_6, LCF,95.0%_7, LCF,99.0%_8
```

Note: although the workbook has 18 individual spectral sheets, the GLOBAL `Summary` sheet only includes five spectral groups: `Lfeq`, `LfFmax`, `LfFmin`, `LfE`, and `LfSmax`.

### PROFILE `Summary` sheet

Rows:

```text
row 1: headers
rows 2-3: blank
row 4 onward: one row per complete PROF record
```

Columns:

```text
1 Period:
2 Time:
3 Duration:
4 blank
5 blank
6 LAFspl
7 LAeq
8 LAFmax
9 LAE
10 LApeak
```

Each individual PROFILE sheet has the same values as one of the five profile columns.

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
5. Sensitivity/calibration field source for the `Setup` sheet.
6. Serial number source for Nortfr-style filenames; not found in SD-card files inspected.
7. The 10 unknown bytes in the `.NBF` wrapper, only needed if generating `.NBF`.
8. Any additional metadata/setup fields needed for a fully faithful Nortfr-style workbook export.

## Historical corpus variation check

Confidence: High for observed corpus.

Historical archive inspected:

```text
/Users/colinives/noise-meter/original.zip
```

Contents:

```text
files: 1103
GLOB files: 516
PROF files: 516
```

Observed `GLOB` sizes:

```text
1069 bytes:  54 files
2653 bytes: 459 files
2668 bytes:   3 files
```

Validation method:

```text
1. Decode direct scalar block around 0x03c1..0x0426 as raw/128 - 20.
2. Decode Lfeq spectrum at 0x0428 where file is long enough.
3. Check scalar LAeq/LCeq against A/C-weighted Lfeq spectrum.
4. Decode paired PROF records as raw/128 - 20 and check profile plausibility.
```

Results by file size:

```text
1069-byte GLOB files:
  count: 54
  scalar block: present and plausible in all 54
  spectrum block: not present; files are too short for 0x0428 + 36 bands
  PROF pairs: present/plausible in all 54, with partial trailing bytes common
  dates observed: 250823, 250824, 250830, 250831

2653-byte GLOB files:
  count: 459
  scalar block: plausible in 456
  spectrum block: present/plausible in 459
  PROF pairs: plausible in 453
  three apparent aborted/error measurements have only 3-byte PROF headers and LAeq/LCE scalar cells at -20

2668-byte GLOB files:
  count: 3
  scalar block: present/plausible in all 3
  spectrum block: present/plausible in all 3
  PROF pairs: present/plausible in all 3
  interpretation: same known layout plus 15 extra bytes
```

The three apparent aborted/error 2653-byte examples:

```text
MEAS118/230614/PART0000/PROJ0008
MEAS118/230911/PART0000/PROJ0008
MEAS118/240418/PART0000/PROJ0008
```

Each has `PROF0008.DAT` length 3 (`00 00 00`) and `LAeq = -20` / `LCE = -20`, indicating no usable equivalent-level measurement despite a normal-size `GLOB`.

Important implementation conclusion:

```text
Use direct GLOB scalar cells for report/global values.
Use GLOB spectra for spectral output and as a diagnostic cross-check.
Do not require scalar LAeq/LCeq to equal spectrum-derived LAeq/LCeq exactly for every historical file.
```

In the historical corpus, scalar-vs-spectrum agreement is usually close for normal files, but not universal. This means the direct scalar block is the authoritative source for the meter's displayed/report global scalar values.

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

The run-9 export plus the screen-value cross-checks for runs 2, 4, 5, 6, 8, 9, and 10 are enough to implement normal 2653-byte `GLOB`/`PROF` measurement exports with high confidence. More exports are only useful for metadata, `.NBF` generation, or unusual file-size/setup edge cases.

## Implementation status (as of 2026-08-15)

The format investigation above has been translated into working app code. The following has been built and deployed to both Pis.

### Database schema additions

The `runs` table has the following additional columns (all added via `ALTER TABLE` in `_run_migrations` so the schema self-upgrades on first run):

```text
-- 18 x 1/3-octave spectral arrays from GLOB (JSON, 36 floats each; NULL for 1069-byte GLOBs)
spec_lfeq, spec_lffmax, spec_lffmin, spec_lfe
spec_lfsmax, spec_lfsmin
spec_lfieq, spec_lfimax, spec_lfimin, spec_lfie
spec_lff_l01, spec_lff_l1, spec_lff_l5, spec_lff_l10
spec_lff_l50, spec_lff_l90, spec_lff_l95, spec_lff_l99

-- 5 x 1-second PROF time series (JSON arrays; NULL until backfilled)
prof_lafspl_json, prof_laeq_json, prof_lafmax_json
prof_lae_json, prof_lapeak_json
```

An `app_settings` key/value table stores user-level configuration:

```sql
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)
```

Currently used for one key: `instrument_serial`.

### Backfill scripts

Two standalone scripts populate historical data for existing runs without re-importing from ZIP:

- **`backfill_glob.py`** — decodes all 18 spectral tables from `GLOB*.DAT` files in the ZIP and writes them to the `spec_*` columns. Sentinel: `spec_lfeq IS NULL`.
- **`backfill_prof.py`** — decodes the 5-channel 1-second time series from `PROF*.DAT` files and writes them to the `prof_*_json` columns. Sentinel: `prof_lafspl_json IS NULL`.

Both use the same decode: `uint16_LE / 128 - 20`, and round to **2** decimal places before
JSON-encoding. Do not reduce this to 1 — see the resolved double-rounding note below.

### Sanity checks run (2026-08-15)

Three-point verification against Nortfr reference for 2026-08-12:

1. **PROF array lengths**: `json_array_length(prof_lafspl_json) = n_samples` for all 11 Gladys August runs. ✓
2. **Spectral integrity**: A-weighted energy sum of `spec_lfeq` matches stored `avg_laeq` to ±0.03 dB for all 10 August-12 runs. ✓
3. **Nortfr reference parity (run 9)**: LAeq=70.8, LApeak=97.4, LCpeak=98.6, LA10=74.8, LA50=67.1, LA90=57.1, LCeq=78.7. All match. ✓

### RESOLVED (2026-08-17): the 0.1 dB PROF discrepancy was our own double rounding

**Status: fixed. PROFILE xlsx now matches the Nortfr reference exactly, 0 differing cells.**

This section previously recorded the cause as a NOR140 hardware rounding convention,
"rounds 0.05 up where Python's `round()` rounds to even". That was wrong on both
counts, and is corrected here because it would mislead anyone re-deriving the format:

- The code never used Python's `round()`. `nor140_format.round_half_up()` has always
  been half-up, so banker's rounding was never in play.
- It was not a hardware quirk at all. It was **double rounding in our own storage.**

Actual cause: `noise_parser.py` stored the PROF series at **1 decimal**, while it stored
the GLOB scalars and spectra at **2**. `nor140_exporter._rv()` then rounds to 1 decimal
on the way out. So GLOBAL took a two-stage path (exact → 2 dp → 1 dp) and matched
Nortfr perfectly, while PROFILE had already lost the intermediate precision and could
only be single-rounded — putting ~5% of values 0.1 dB low.

Nortfr evidently applies the same two-stage rounding. Worked example, first sample of
run 9:

```text
raw word 12857  ->  12857/128 - 20  =  80.4453125 dB exact
  single stage:  round_half_up(80.4453125, 1)                 = 80.4   (was stored)
  two stage:     round_half_up(round_half_up(…, 2), 1)
                 80.4453125 -> 80.45 -> 80.5                  = 80.5   (Nortfr)
```

Confirmed by three independent lines of evidence:

1. Applying the two-stage rounding to all 4,500 raw values of run 9 reproduces the
   entire Nortfr PROFILE workbook with **zero** differences, against 222 under single
   rounding.
2. The affected rate is **4.933%** (222/4500), against a predicted **5.000%** — double
   rounding promotes exactly those values whose fraction falls in `[0.045, 0.050)`,
   which is 5% of each 0.1 dB step.
3. The code asymmetry predicts the result asymmetry exactly: 2 dp storage → GLOBAL
   matched; 1 dp storage → PROFILE did not.

**Fix applied:** store the PROF series at 2 decimals in both `noise_parser.py` and
`backfill_prof.py`. The exporter needed no change — feeding it 2 dp values *is* the
two-stage path. Pinned by `test_modules.py`, which now asserts 0 differing cells
against both reference workbooks.

Worth knowing: single rounding was in fact marginally *more* accurate (mean error
0.0469 dB against Nortfr's 0.0531 dB, closer in all 222 cases), since double rounding
amplifies error. That was not the reason to change it. The reason is that storing only
1 decimal discarded precision needed by everything derived from the profile — the
pooled LA50 for run 9 came out 67.0, where the meter's own GLOB percentile says 67.1.
At 2 dp it agrees. Vendor-exact export came along for free.

### xlsx exporter (`nor140_exporter.py`)

Generates Nortfr-compatible GLOBAL (58 sheets) and PROFILE (7 sheets) workbooks from stored run data.

Public API:

```python
from nor140_exporter import build_global_xlsx, build_profile_xlsx, export_filename

run = noise_db.get_full_run_row(date, run_number)  # includes session_date
serial = noise_db.get_setting('instrument_serial', '')

xlsx_bytes = build_global_xlsx(run, serial)
xlsx_bytes = build_profile_xlsx(run, serial)
fname = export_filename(run, serial, 'GLOBAL')
# → 'NOR140_6899108_260812_0009_GLOBAL.xlsx'
```

`get_full_run_row()` joins the sessions table to add `session_date`, needed because `start_time` in the `runs` table is stored as a bare time string (`'23:27:36'`), not a full datetime.

**Important**: `step` in the run dict is a chart-downsampling factor, not a multiplier on measurement time. Actual run duration is always `n_samples` seconds (one measurement per second). The PROFILE period length is always 1 second.

**Dependency**: `openpyxl` — installed on both Pis with `pip3 install openpyxl --break-system-packages`.

### Flask route

```
GET /session/<date>/run/<run_number>/export/nor140/<GLOBAL|PROFILE>
```

Returns the xlsx file as a download. Serial number is read from `app_settings`. Only accessible when logged in.

### UI

Download links ("NOR140 Global xlsx" / "NOR140 Profile xlsx") appear at the bottom of each run card in the session view, but only for runs that have GLOB-derived scalar data (`lafmax IS NOT NULL`). Links use `onclick="event.stopPropagation()"` to prevent the card expand handler from firing.

The instrument serial number can be set on the Manage page (above the session list).

### Nortfr xlsx structure details (confirmed by comparison)

Points that were surprising or non-obvious during reverse engineering:

**Cell layout — all sheets:**

- All data rows and column-header rows have `None` at column index 2 (zero-based). Values start at column 3.
  - Data: `[period_idx, datetime_str, None, value(s)]`
  - Header: `['Period:', 'Time:', None, col_label(s)]`
- The 8-row header block has TWO blank rows (indices 6 and 7), not one.
- Spectral sheets: row 7 has the sheet label at column 3: `[None, None, None, 'Lfeq']`.
- Non-spectral sheets: row 7 is fully blank.
- Data starts at row 9 for all sheet types.

**Header row formats:**

- Row 0 / row 5: value in column 1, format hint in column 2: `['Period length', '(0:15:0.0)', 'H:M:S.mS']`
- Row 4 trig time: `['Trig time', '(2026/8/12 23:27:36.0)', 'Y-Mo-D H:M:S.mS']` — slashes, no zero-padding on month/day, `.0` milliseconds
- Data row datetimes: `(2026-08-12 23:27:36.000)` — dashes, `.000` milliseconds
- Duration format: `(H:M:S.0)` — no zero-padding on any component, e.g. `(0:15:0.0)` not `(0:15:00.0)`

**GLOBAL vs PROFILE period length:**

- GLOBAL row 0: total run duration, e.g. `(0:15:0.0)` — one "period" = entire run
- PROFILE row 0: `(0:0:1.0)` — one period = one second
- Both have the same effective duration in row 5

**Summary sheet** — does not use the standard 8-row header block:

```text
row 0: ['Period:', 'Time:', 'Duration:', None, None, scalar_headers...]
row 1: blank
row 2: blank
row 3: [0, datetime, None, None, None, scalar_vals...]
row 4: blank
row 5: blank
row 6+: NC / NR / RC II rating curves (not reproduced in app export)
```

**Frequency column labels** — switch from Hz to kHz above 800 Hz:

```text
'6.3 Hz', '8.0 Hz', '10 Hz', ..., '800 Hz',
'1.0 kHz', '1.25 kHz', ..., '20.0 kHz'
```

Note `8.0 Hz` (not `8 Hz`) and the decimal formatting above 1 kHz.

**Sheet naming** — note capital letters: `LfSmin`, `LfSmax`, `LfFmin`, `LfFmax` (not `Lfsmin` etc.).

**Percentile sheet order** — both LCF and LAF groups run in descending order: `99.0%_8` → `0.1%_1` (sheets 18–33).

### Comparison result (run 9, 2026-08-15)

| Check | Result |
|---|---|
| 58 GLOBAL sheets in correct order | ✓ |
| 7 PROFILE sheets in correct order | ✓ |
| All scalar values (LAeq, LApeak, LA10/50/90, LCeq, LCpeak, LAE, LCE…) | ✓ exact match |
| Lfeq 36 spectral bands | 35/36 exact, 1 band off by 0.1 dB (decode rounding) |
| Header formats (period length, trig time, blank rows, col layout) | ✓ exact match |
| PROFILE 900 data rows | ✓ 0 mismatches >0.1 dB |

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
