# Breaker and Cable Calculator (pyRevit, Revit 2025)

**Electrical tab → Calculations → Breaker Cable Calc**

Preliminary circuit-breaker and cable calculations for panel boards with mixed
lighting, socket and equipment loads: single-phase panels, three-phase panels,
and single-phase / two-pole / three-phase branch circuits on a three-phase panel.

> **All results are preliminary and require review by a qualified electrical
> engineer.** The tool does not confirm code compliance and is not an
> engineering approval. It contains **no** SBC clause references, demand
> factors, cable ampacities, correction factors, voltage-drop limits, breaker
> ratings, fault levels or disconnection times. Every such value comes from the
> project data file that you fill in and verify.

## 1. Install

1. Copy the folder `CableCalc.extension` to a folder that pyRevit searches for
   extensions (pyRevit tab → Settings → Custom Extension Directories → add the
   **parent** folder of `CableCalc.extension`).
2. pyRevit tab → Reload. The button appears on the **Electrical** tab.
3. To write results to the model, add the parameters you map (section 4).
   `BCC_SharedParameters.txt` has 12 ready-made **Text** parameters:
   Manage → Project Parameters → Add → Shared parameter → bind
   the first eight to **Electrical Circuits**, `BCC_L1/L2/L3 Current`,
   `BCC_Phase Imbalance` and `BCC_Note` to **Electrical Equipment**, as Instance.

Files in the button folder: `script.py` (start), `bcc_window.py` + `ui.xaml`
(window), `bcc_config.py` (project data file), `bcc_calc.py` (formulas),
`bcc_report.py` (tables/text), `bcc_sample.py` (CSV circuits), `bcc_revit.py`
(read model, write mapped parameters), `sample_config.json`,
`sample_circuits.csv`.

## 2. How it works

1. **Project data file** – choose your JSON file. The first time, the example
   file is loaded; the banner then says *EXAMPLE VALUES* and writing is off.
2. **Tick panels** – panels selected in Revit are pre-ticked.
3. **Calculate (preview only)** – reads the model, changes nothing.
4. Review the tabs:
   1. *Inputs to review* – every value used per circuit, missing/invalid inputs,
      defaults used, and the excluded elements (spares, spaces, non-power circuits).
   2. *Results* – IB, In, cable, IZ, voltage drop, each check with its status.
   3. *Phases* – load and current on L1 / L2 / L3, imbalance, neutral note.
   4. *Data used and not checked* – the project data with sources, formulas,
      and the list of what the tool does not check.
   5. *Proposed updates to the model* – element, parameter, present value,
      new value, and whether it can be written.
5. **Copy summary / Export** – text summary to the clipboard, or a results CSV
   plus `<name>_summary.txt`.
6. **Write to model…** – asks for confirmation, then writes the READY values in
   one transaction. The State column then shows WRITTEN / FAILED for each value.

Statuses: `PASS`, `FAIL`, `MISSING INPUT`, `NOT CHECKED`. The overall result of a
circuit is `FAIL`, `MISSING INPUT` or `REVIEW`. `REVIEW` means none of the listed
checks failed – it is not an approval.

## 3. What is calculated

| Item | Formula |
|---|---|
| Design current, 1-phase | IB = P × fd / (V_LN × PF × η) |
| Design current, 3-phase | IB = P × fd / (√3 × V_LL × PF × η) |
| Design current, 2-pole | IB = P × fd / (V × PF × η), V = V_LL or V_LN per `system.two_pole_voltage` |
| Cable ampacity | IZ = I_table × product of the correction factors |
| Overload coordination | IB ≤ In ≤ IZ |
| Conventional operating current | I2 ≤ 1.45 × IZ – only when `breakers.i2_ratio` is given |
| Voltage drop, 3-phase | √3 × I × L × (R cos φ + X sin φ) |
| Voltage drop, 1-phase / 2-pole | 2 × I × L × (R cos φ + X sin φ) |
| Phase current | arithmetic sum of the IB of the circuits on that phase |
| Phase imbalance | max \|I_phase − I_average\| / I_average × 100 % |

Assumptions that are stated in the results:

- A three-phase circuit is taken as balanced (one third of its power per phase,
  IB on each phase). A two-pole circuit puts IB on both of its phases.
- Phase currents are **arithmetic** sums; with different power factors the true
  phasor sum can be lower.
- Phase assignment is **never inferred**. A circuit without a usable phase is
  calculated but left out of the phase totals and flagged, and the imbalance is
  then reported as `MISSING INPUT`.
- **Neutral current is not calculated.** The neutral is flagged for separate
  verification, naming the imbalance and any load classification you listed
  as nonlinear.

**Not checked** (always listed in the tool): short-circuit withstand of the
cable, earth-fault loop impedance and disconnection time, selectivity, inrush /
motor starting, neutral and harmonic loading, grouping changes along the route.
Breaking capacity is compared (Icu ≥ fault current) only when both
`breakers.icu_ka` and a fault current for the circuit are given; otherwise
`NOT CHECKED`. Phase balance is only the simple comparison above.

## 4. Project data file (JSON)

Copy `sample_config.json`, replace **every** value with verified project data,
fill the `source` texts, enter `project.code_edition`, then set
`"example_only": false`. Writing to the model stays off while `example_only` is
true or the edition is empty. Keys starting with `_` are comments.

| Key | Meaning |
|---|---|
| `example_only` | `true` = example data; writing is blocked |
| `project.code_edition` | The applicable SBC edition, as text you enter |
| `system.phases`, `v_ll`, `v_ln` | Supply of the panels (1 or 3 phases, volts) |
| `system.two_pole_voltage` | `"LL"` or `"LN"` for two-pole circuits |
| `system.panels` | Per-panel override by panel name, e.g. a single-phase panel |
| `defaults.power_factor / efficiency / demand_factor / correction_set` | Used only when the circuit has no value; each use is noted. `null` = no default |
| `demand_factors` | Load classification text → demand factor |
| `breakers.selection` | `"propose"` (smallest listed rating ≥ IB) or `"use_model_rating"` |
| `breakers.ratings_a` | The ratings available on the project |
| `breakers.i2_ratio`, `icu_ka` | Device data; `null` → that check is `NOT CHECKED` |
| `cables[]` | `name`, `i_table_a`, `r_ohm_per_km`, `x_ohm_per_km`, `poles`, `source` – smallest first |
| `correction_factors` | Named sets of `{name, value, source}` |
| `voltage_drop.current` | `"IB"` or `"In"` |
| `voltage_drop.limit_percent` | Limit; `null` → calculated but `NOT CHECKED` |
| `voltage_drop.include_in_cable_selection` | `true` = a cable over the limit is not chosen |
| `phase_balance.flag_above_percent` | Project criterion; `null` → `NOT CHECKED` |
| `phase_balance.nonlinear_classifications` | Classifications named in the neutral warning |
| `revit.read.*` | Where each input comes from: `<TrueLoad>`, `<PowerFactor>`, `<Length>`, `<LoadClassification>`, `<PhaseLoads>`, or a parameter name on the circuit, or `null` |
| `revit.phase_labels` | How phase letters map to L1/L2/L3, e.g. `{"A":"L1","B":"L2","C":"L3"}` |
| `revit.write.circuit / panel` | Result → parameter name. **Only these parameters are written** |

Phase source: `<PhaseLoads>` uses the phases on which Revit itself reports load
for the circuit (from the panel schedule slots). Alternatively map a text
parameter holding e.g. `L1`, `L1-L2`, `A,B,C`.

Parameters to write can be Text (recommended), Number, or – for currents –
Current. Other types are reported as UNSUPPORTED and not written. Missing and
read-only parameters are reported, and a result that has no value leaves the
present parameter value as it is (reported as NO VALUE).

## 5. Sample data

`sample_circuits.csv` + `sample_config.json`: **example values only – not design
recommendations**. In the window choose *Circuits from → A CSV file* and press
Cancel in the file dialog to load them. CSV results are never written to the model.

## 6. Verification guide (do this before relying on the tool)

1. **Formulas** – load the sample CSV with the example file. Circuit 1:
   IB = 1800 / (230 × 0.9) = 8.70 A; IZ = 15 × 0.9 × 0.8 = 10.8 A;
   drop = 2 × 8.70 × 0.025 × (14 × 0.9 + 0.10 × 0.436) = 5.50 V = 2.39 %.
   Circuit 3: IB = 15000 × 0.8 / (√3 × 400 × 0.85 × 0.9) = 22.64 A.
   Compare with tab 2.
2. **Missing data** – circuits 4, 6, 7 and 9 of the sample must show a missing
   phase, `MISSING INPUT` for voltage drop, and `MISSING INPUT` for demand
   factor and power factor.
3. **Your data** – open tab 4 and check every value and source against the
   documents they came from. Lines marked *NO SOURCE GIVEN* need one.
4. **Model inputs** – on a test panel compare tab 1 with the panel schedule:
   load, power factor, poles, length, classification and especially the
   **phase of every circuit**.
5. **Hand check** – recalculate at least one single-phase, one two-pole and one
   three-phase circuit by hand, and the three phase totals.
6. **Write test** – on a copy of the model: Calculate, review tab 5, write, and
   check the State column and the parameters in a schedule. Undo removes the
   whole write (one transaction).

## 7. Test status

Checked outside Revit with IronPython 2.7 against a simulated model (102 checks:
formulas against hand arithmetic, the sample data, statuses, reading, proposed
updates, writing, the window). **Not yet run inside Revit 2025.** The first run
in Revit should follow section 6, steps 4–6.
