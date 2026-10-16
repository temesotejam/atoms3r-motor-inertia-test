"""Compare independent position-change kinematics against RPM readback.

Fixed assembly only. Centered local fits are offline estimates with explicitly
recorded window widths. Centroid difference is a descriptive delay measure,
not identification of unique sensor, motor, current-filter or friction models.
"""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from analyze_response import read_log


def derivatives(t_us, raw_cdeg, period_us):
    t = np.asarray(t_us, dtype=float)
    raw = [int(x) for x in raw_cdeg]
    y = np.zeros(len(raw))
    for k in range(1,len(raw)):
        delta = (raw[k]-raw[k-1]+2**31) % 2**32-2**31
        y[k] = y[k-1]+delta/100
    result=[]
    for k in range(2,len(t)-2):
        tt=t[k-2:k+3]
        if np.any(np.diff(tt)<=0) or np.max(np.diff(tt))>2.5*period_us:
            continue
        # A legacy cumulative-position signal must not wrap each revolution.
        # Reject a jump rather than choosing a convenient 360-deg unwrap.
        if np.any(np.abs(np.diff(y[k-2:k+3]))>2500*6*np.diff(tt)/1e6+1):
            continue
        x=(tt-t[k])/1e6
        coeff=np.polyfit(x,y[k-2:k+3],2)
        rmse=float(np.sqrt(np.mean((np.polyval(coeff,x)-y[k-2:k+3])**2)))
        if rmse>.5:
            continue
        result.append(dict(t_us=float(t[k]),position_rpm=float(coeff[1]/6),
            position_alpha_rad_s2=float(2*coeff[0]*np.pi/180),
            position_fit_rmse_deg=rmse,position_fit_span_ms=float((tt[-1]-tt[0])/1000)))
    return result


def integral(y,x):
    return float(np.sum((y[1:]+y[:-1])*.5*np.diff(x)))


def centroid(t_us,y):
    t=(np.asarray(t_us)-t_us[0])/1e6
    y=np.maximum(np.asarray(y),0)
    area=integral(y,t)
    return (integral(y*t,t)/area if area>0 else None),area


def analyze(paths,inertia=2.8e-5):
    if not np.isfinite(inertia) or inertia<=0:
        raise ValueError('inertia must be finite and positive')
    pulse_rows,trace_rows,sources=[],[],[]
    seen=set()
    for path in paths:
        path=Path(path);digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            raise ValueError('duplicate input file content')
        seen.add(digest)
        meta,table=read_log(path)
        if meta.get('schema')!='atoms3r_inertia_v2':
            raise ValueError('position comparison requires CSV v2; old logs lack position')
        sources.append(dict(file=str(path),sha256=digest,motor_firmware=meta.get('motor_firmware'),
                            profile=meta.get('profile'),result=meta.get('result')))
        groups=defaultdict(list)
        for r in table:groups[int(r['pulse_id'])].append(r)
        for pid,rows in groups.items():
            first=rows[0];on=int(first['pulse_on_us']);off=int(first['pulse_off_us']);zero=int(first['pulse_zero_us'])
            summary=dict(source=path.name,pulse_id=pid,command_mA=int(first['pulse_cmd_mA']),
                width_ms=(zero-on)/1000 if zero>on else None,status='not_analyzed')
            pulse_rows.append(summary)
            if meta.get('result')!='done' or not int(first['pulse_complete']) or not 0<on<zero<=off:
                summary['status']='run_or_pulse_incomplete';continue
            pos=[r for r in rows if int(r['position_valid']) and
                 0<=int(r['position_end_us'])-int(r['position_start_us'])<=3000 and
                 on-400000 <= (int(r['position_start_us'])+int(r['position_end_us']))/2 <= off+int(meta.get('post_period_us',2000000))]
            speed=[r for r in rows if int(r['speed_valid']) and
                   0<=int(r['speed_end_us'])-int(r['speed_start_us'])<=3000]
            if len(pos)<9 or len(speed)<9:
                summary['status']='insufficient_position_or_speed';continue
            period=int(meta['sample_period_us'])
            kin=derivatives([(int(r['position_start_us'])+int(r['position_end_us']))/2 for r in pos],
                            [r['position_raw_cdeg'] for r in pos],period)
            if len(kin)<5:
                summary['status']='position_not_usable';continue
            tt=np.array([x['t_us'] for x in kin]);vv=np.array([x['position_rpm'] for x in kin])
            rt=np.array([(int(r['speed_start_us'])+int(r['speed_end_us']))/2 for r in speed])
            rv=np.array([float(r['rpm']) for r in speed])
            if np.any(np.diff(rt)<=0):
                summary['status']='nonmonotonic_speed_clock';continue
            reference=np.interp(tt,rt,rv,left=np.nan,right=np.nan)
            robust=(np.abs(vv)>20)&(np.abs(reference)>10)&np.isfinite(reference)
            if np.sum(robust)<5:
                summary['status']='position_motion_not_observed' if np.max(np.abs(vv))<1 else 'insufficient_motion_for_direction_comparison'
                continue
            orientation=1 if np.median(vv[robust]*reference[robust])>0 else -1
            agreement=float(np.mean(orientation*vv[robust]*reference[robust]>0))
            summary['position_to_rpm_sign']=orientation
            summary['direction_agreement_fraction']=agreement
            if agreement<.95:
                summary['status']='position_speed_direction_inconsistent';continue
            vv=vv*orientation
            sign=1 if summary['command_mA']>0 else -1
            eligible=(tt>=on)&(tt<=off+int(meta.get('post_period_us',2000000)))&np.isfinite(reference)
            indexes=np.where(eligible)[0]
            if len(indexes)<5:
                summary['status']='insufficient_comparison_window';continue
            k=indexes[np.argmax(sign*vv[indexes])]
            sr=(rt>=on)&(rt<=tt[indexes[-1]])
            peak_speed_time=rt[sr][np.argmax(sign*rv[sr])]
            summary.update(status='position_comparison_available',
                position_peak_rpm=float(sign*vv[k]),speed_peak_rpm=float(np.max(sign*rv[sr])),
                position_peak_after_off_ms=float((tt[k]-off)/1000),
                speed_peak_after_off_ms=float((peak_speed_time-off)/1000),
                peak_time_difference_ms=float((peak_speed_time-tt[k])/1000),
                fit_span_ms_median=float(np.median([x['position_fit_span_ms'] for x in kin])))
            if tt[0]<=on and tt[-1]>=zero:
                omega_on=float(np.interp(on,tt,vv))*2*np.pi/60
                omega_zero=float(np.interp(zero,tt,vv))*2*np.pi/60
                summary['delta_omega_during_command_rad_s']=omega_zero-omega_on
                summary['conditional_delta_angular_momentum_nms']=inertia*(omega_zero-omega_on)
            tail=indexes[tt[indexes]>=tt[indexes[-1]]-100000]
            tail_stopped=np.max(np.abs(vv[tail]))<10 and np.max(np.abs(reference[tail]))<10
            summary['comparison_tail_below_10rpm']=bool(tail_stopped)
            # A gap is retained as unavailable; do not fill missing speed data
            # and then present the first-moment difference as a filter constant.
            rt_local=rt[(rt>=tt[indexes[0]])&(rt<=tt[indexes[-1]])]
            continuous=(np.max(np.diff(tt[indexes]))<=2.5*period and len(rt_local)>1 and
                        np.max(np.diff(rt_local))<=2.5*period)
            if tail_stopped and continuous:
                pc,pa=centroid(tt[indexes],sign*vv[indexes])
                rc,ra=centroid(tt[indexes],sign*reference[indexes])
                summary['speed_area_to_position_area_ratio']=ra/pa if pa else None
                summary['descriptive_centroid_difference_ms']=(rc-pc)*1000 if pc is not None and rc is not None else None
            for k,x in enumerate(kin):
                x=dict(x,source=path.name,pulse_id=pid,time_from_on_ms=(x['t_us']-on)/1000,
                    time_from_off_ms=(x['t_us']-off)/1000,position_to_rpm_sign=orientation,
                    aligned_position_rpm=float(vv[k]),speed_readback_rpm=float(reference[k]) if np.isfinite(reference[k]) else None,
                    aligned_position_alpha_rad_s2=x['position_alpha_rad_s2']*orientation,
                    conditional_net_rotor_torque_nm=inertia*x['position_alpha_rad_s2']*orientation)
                trace_rows.append(x)
    return dict(revision='position_speed_comparison_v1_20261016',sources=sources,
        inertia_kg_m2=inertia,inertia_provisional=True,absolute_inertia_identified=False,
        body_work_measured=False,unique_delay_or_friction_model_identified=False,
        position_is_independent_sensor=False,position_firmware_path_requires_hardware_validation=True,
        pulse_summary=pulse_rows,position_kinematics=trace_rows)


def write_csv(path,rows):
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files',nargs='+',type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--inertia',default=2.8e-5,type=float)
    args=parser.parse_args();report=analyze(args.files,args.inertia)
    args.out.mkdir(parents=True,exist_ok=True)
    (args.out/'position_response.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    write_csv(args.out/'position_pulse_summary.csv',report['pulse_summary'])
    write_csv(args.out/'position_kinematics.csv',report['position_kinematics'])
    print(json.dumps({k:v for k,v in report.items() if k!='position_kinematics'},ensure_ascii=False,indent=2))
