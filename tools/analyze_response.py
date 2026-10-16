"""Fit fixed-assembly current -> acceleration response; J requires an independent Kt.

python tools/analyze_response.py run1.csv run2.csv --out analysis
python tools/analyze_response.py run1.csv --kt 0.05 --kt-source "explicit assumption" --out analysis
"""
import argparse
import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np


def read_log(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    meta = {}
    for line in text.splitlines():
        if line.startswith('# ') and '=' in line:
            key, value = line[2:].split('=', 1)
            meta[key] = value
    if meta.get('schema') not in ('atoms3r_inertia_v1', 'atoms3r_inertia_v2'):
        raise ValueError(f'{path}: unsupported CSV schema')
    if meta.get('configuration') != 'current_wheel_and_weights':
        raise ValueError(f'{path}: different wheel configuration')
    table = list(csv.DictReader(io.StringIO('\n'.join(x for x in text.splitlines() if not x.startswith('#')))))
    if len(table) != int(meta.get('sample_count', -1)) or not table:
        raise ValueError(f'{path}: incomplete CSV / sample count mismatch')
    if [int(x['sample']) for x in table] != list(range(len(table))):
        raise ValueError(f'{path}: sample sequence is not contiguous')
    if any(int(b['t_us']) <= int(a['t_us']) for a, b in zip(table, table[1:])):
        raise ValueError(f'{path}: non-monotonic sample timestamps')
    return meta, table


def pulse_windows(meta, table, path, trim_ms=200, width_ms=60):
    groups = defaultdict(list)
    for row in table:
        groups[int(row['pulse_id'])].append(row)
    diagnostics, windows = [], []
    for pid, rows in groups.items():
        first = rows[0]
        on = int(first['pulse_on_us'])
        zero = int(first['pulse_zero_us'])
        off = int(first['pulse_off_us'])
        cmd = int(first['pulse_cmd_mA'])
        sign = 1 if cmd > 0 else -1
        d = {'run_id': meta['run_id'], 'source': str(path), 'pulse_id': pid,
             'command_mA': cmd, 'complete': int(first['pulse_complete']),
             'actual_width_ms': (zero-on)/1000 if zero > on > 0 else None,
             'accepted_windows': 0, 'rejection': ''}
        diagnostics.append(d)
        if meta.get('result') != 'done':
            d['rejection'] = 'run_not_completed'; continue
        if not (d['complete'] and on > 0 and zero > on and off >= zero):
            d['rejection'] = 'pulse_not_complete'; continue
        if abs((zero-on)/1000 - float(first.get('pulse_width_ms_setting', meta['pulse_width_ms']))) > 10:
            d['rejection'] = 'pulse_timing_error'; continue
        valid = [r for r in rows if all(int(r[k]) for k in ('speed_valid', 'current_valid', 'status_valid'))
                 and not int(r['motor_error'])]
        speed_t = np.array([(int(r['speed_start_us'])+int(r['speed_end_us']))/2 for r in valid])
        current_t = np.array([(int(r['current_start_us'])+int(r['current_end_us']))/2 for r in valid])
        rpm = np.array([float(r['rpm']) for r in valid])
        current = np.array([float(r['actual_current_mA']) for r in valid])
        if len(valid) < 5 or np.any(np.diff(speed_t) <= 0) or np.any(np.diff(current_t) <= 0):
            d['rejection'] = 'insufficient_or_duplicate_telemetry'; continue
        pulse_speed = (speed_t >= on) & (speed_t <= zero)
        d['rpm_change_during_pulse'] = float(rpm[pulse_speed][-1]-rpm[pulse_speed][0]) if sum(pulse_speed) >= 2 else None
        d['mean_voltage_mV'] = float(np.mean([float(r['voltage_mV']) for r in valid]))
        # Keep speed and current on their own I2C acquisition clocks. Only a
        # window fully supported by both series is allowed; no extrapolation.
        for start in np.arange(on+trim_ms*1000, zero-20000-width_ms*1000+1, width_ms*1000):
            end = start+width_ms*1000
            mask = (speed_t >= start) & (speed_t <= end)
            ts, ys = speed_t[mask], rpm[mask]*sign*2*np.pi/60
            if len(ts) < 5 or np.max(np.diff(ts)) > 25000:
                continue
            if ts[0] < current_t[0] or ts[-1] > current_t[-1]:
                continue
            cmask = (current_t >= ts[0]) & (current_t <= ts[-1])
            if not np.any(cmask) or np.max(np.diff(current_t[(current_t >= ts[0]-15000) & (current_t <= ts[-1]+15000)]), initial=0) > 25000:
                continue
            raw_i = np.interp(ts, current_t, current)
            # Readback filtering is not inverted. Reject non-steady windows,
            # and use only the late part of a pulse for the physical fit.
            ia = abs(raw_i)/1000
            if np.ptp(ia) > max(.005, .1*float(np.mean(ia))) or np.mean(ia) < .01:
                continue
            if np.mean(ys)*60/(2*np.pi) < 30:
                continue
            tt = (ts-ts[0])/1e6
            slope, intercept = np.polyfit(tt, ys, 1)
            residual = ys-(intercept+slope*tt)
            r2 = 1-float(residual@residual)/max(float(np.sum((ys-np.mean(ys))**2)), 1e-12)
            if slope <= 0 or r2 < .95:
                continue
            windows.append({'run_id': meta['run_id'], 'pulse_id': pid, 'sign': sign,
                            'command_abs_mA': abs(cmd), 'start_us': float(ts[0]), 'end_us': float(ts[-1]),
                            'current_A': float(np.mean(ia)), 'omega_rad_s': float(np.mean(ys)),
                            'alpha_rad_s2': float(slope), 'speed_fit_r2': r2})
            d['accepted_windows'] += 1
        if not d['accepted_windows']:
            d['rejection'] = 'no_late_steady_window_short_pulse_or_no_motion'
    return diagnostics, windows


def fit_response(windows):
    levels = defaultdict(set)
    for w in windows:
        levels[w['command_abs_mA']].add((w['run_id'], w['pulse_id']))
    if len(levels) < 3 or min(map(len, levels.values()), default=0) < 2:
        raise ValueError('three current levels, with at least two accepted pulses per level, are required')
    x = np.array([[w['current_A'], w['omega_rad_s']] for w in windows])
    y = np.array([w['alpha_rad_s2'] for w in windows])
    mean, scale = x.mean(axis=0), x.std(axis=0)
    if np.any(scale < 1e-8):
        raise ValueError('insufficient current or speed variation')
    design = np.column_stack(((x-mean)/scale, np.ones(len(x))))
    beta, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
    condition = float(np.linalg.cond(design))
    if rank < 3 or condition > 100:
        raise ValueError('current and speed effects cannot be separated in these data')
    a, c = beta[:2]/scale
    b = beta[2]-a*mean[0]-c*mean[1]
    if a <= 0:
        raise ValueError('nonpositive current-response coefficient')
    prediction = a*x[:, 0]+b+c*x[:, 1]
    return {'current_coefficient_rad_s2_per_A': float(a), 'offset_rad_s2': float(b),
            'speed_coefficient_per_s': float(c), 'rmse_rad_s2': float(np.sqrt(np.mean((prediction-y)**2))),
            'design_condition': condition, 'windows': len(windows),
            'pulses': len({(w['run_id'], w['pulse_id']) for w in windows}),
            'current_A_range': [float(x[:, 0].min()), float(x[:, 0].max())],
            'rpm_range': [float(x[:, 1].min()*60/(2*np.pi)), float(x[:, 1].max()*60/(2*np.pi))]}


def analyze(paths, trim_ms=200, window_ms=60, kt=None, kt_source=None):
    if kt is not None and (kt <= 0 or not kt_source):
        raise ValueError('a positive Kt and an explicit --kt-source are required')
    diagnostics, windows, inputs, ids = [], [], [], set()
    for path in paths:
        meta, table = read_log(path)
        if meta['run_id'] in ids:
            raise ValueError('duplicate run_id: do not include the same measurement twice')
        ids.add(meta['run_id'])
        d, w = pulse_windows(meta, table, path, trim_ms, window_ms)
        diagnostics.extend(d); windows.extend(w)
        inputs.append({'path': str(path), 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(), 'metadata': meta})
    models = {}
    for sign in (1, -1):
        group = [w for w in windows if w['sign'] == sign]
        key = 'positive' if sign > 0 else 'negative'
        try:
            model = fit_response(group)
            if kt is not None:
                model['conditional_J_kg_m2'] = kt/model['current_coefficient_rad_s2_per_A']
                model['Kt_assumed_Nm_per_A'] = kt
                model['Kt_source'] = kt_source
            model['status'] = 'empirical_response_fitted'
            model['physical_model_warnings'] = []
            if model['speed_coefficient_per_s'] > 0:
                model['physical_model_warnings'].append('speed term indicates acceleration growth, not passive viscous loss')
            if model['offset_rad_s2'] > 0:
                model['physical_model_warnings'].append('positive offset does not represent resisting friction')
            if kt is not None and model['conditional_J_kg_m2'] < 2.04572e-5:
                model['physical_model_warnings'].append('conditional inertia is below the historical external-wheel estimate; review current/Kt conventions and the fitted model')
            models[key] = model
        except ValueError as error:
            models[key] = {'status': 'insufficient_data', 'reason': str(error)}
    result = {'schema': 'atoms3r_current_response_analysis_v1', 'inputs': inputs,
              'trim_ms': trim_ms, 'window_ms': window_ms, 'model': 'alpha=A*abs(I_readback_A)+B+C*abs(omega_rad_s), fit separately by direction',
              'absolute_inertia_identified': False,
              'interpretation': 'A approximates Kt/J only if readback current is proportional to torque and the constant-plus-viscous loss model holds. J is not independently identified. Any conditional_J scales linearly with the supplied Kt.',
              'models': models, 'pulse_rejections': dict(Counter(d['rejection'] for d in diagnostics if d['rejection']))}
    return result, diagnostics, windows


def write_table(path, rows):
    if not rows:
        Path(path).write_text('', encoding='utf-8'); return
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('csv', nargs='+', type=Path)
    p.add_argument('--out', type=Path, default=Path('analysis'))
    p.add_argument('--trim-ms', type=float, default=200)
    p.add_argument('--window-ms', type=float, default=60)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--kt', type=float, help='independent or explicitly assumed torque constant, Nm/A')
    group.add_argument('--nominal-kv', type=float, help='optional uncalibrated SI-convention scenario: Kt=60/(2*pi*Kv)')
    p.add_argument('--kt-source', help='source and status of the supplied torque constant')
    args = p.parse_args()
    if args.trim_ms < 0 or args.window_ms < 40:
        p.error('trim must be nonnegative; window must be at least 40 ms')
    if args.nominal_kv is not None:
        if args.nominal_kv <= 0:
            p.error('nominal Kv must be positive')
        args.kt = 60/(2*np.pi*args.nominal_kv)
        args.kt_source = f'ASSUMPTION: nominal {args.nominal_kv:g} rpm/V, matched SI voltage/current conventions. Not a calibrated Roller485 torque constant; q-axis/phase/line conventions are unverified.'
    try:
        result, pulses, windows = analyze(args.csv, args.trim_ms, args.window_ms, args.kt, args.kt_source)
    except (ValueError, KeyError) as error:
        p.error(str(error))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/'response_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    write_table(args.out/'pulse_summary.csv', pulses)
    write_table(args.out/'accepted_windows.csv', windows)
    print(json.dumps(result['models'], ensure_ascii=False, indent=2))
    print('Absolute inertia is NOT independently identified. See response_summary.json.')


if __name__ == '__main__':
    main()
