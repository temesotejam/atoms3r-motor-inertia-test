import csv
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from analyze_position_response import analyze,derivatives


def make_log(path,position_sign=1,constant=False,complete=True):
    rows=[];readback=0.;dt=.005;alpha=100.;on=1000000;zero=1100000;off=zero+500
    for i,t in enumerate(range(0,4200000,5000)):
        age=(t-on)/1e6
        if age<0:theta=velocity=0.
        elif age<=.1:theta=.5*alpha*age**2;velocity=alpha*age
        else:
            theta=.5*alpha*.1**2+alpha*.1*.2*(1-math.exp(-(age-.1)/.2))
            velocity=alpha*.1*math.exp(-(age-.1)/.2)
        rpm=velocity*60/(2*math.pi)
        readback+=(1-math.exp(-dt/.08))*(rpm-readback)
        raw=round(position_sign*math.degrees(theta)*10)*10 if not constant else 0
        row=dict(sample=i,t_us=t+700,pulse_id=1,phase='pulse' if on<=t<zero else 'post' if t>=zero else 'pre',
            driver_cmd_mA=300 if on<=t<zero else 0,output_enabled=int(on<=t<zero),
            rpm=readback,actual_current_mA=300 if on<=t<zero else 0,
            speed_start_us=t+100,speed_end_us=t+200,current_start_us=t+250,current_end_us=t+350,
            position_start_us=t,position_end_us=t+80,position_raw_cdeg=raw,position_deg=raw/100,
            position_valid=1,speed_valid=1,current_valid=1,status_valid=1,motor_error=0,
            pulse_on_us=on,pulse_zero_us=zero,pulse_off_us=off,pulse_complete=int(complete),
            pulse_cmd_mA=300,pulse_width_ms_setting=100)
        rows.append(row)
    with path.open('w',encoding='utf-8',newline='') as f:
        for key,value in dict(schema='atoms3r_inertia_v2',configuration='current_wheel_and_weights',
            sample_count=len(rows),sample_period_us=5000,post_period_us=3000000,
            result='done' if complete else 'fault',pulse_width_ms=100,run_id='synthetic',motor_firmware=4).items():
            f.write(f'# {key}={value}\n')
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


class PositionResponseTests(unittest.TestCase):
    def test_constant_velocity_and_signed_counter_rollover(self):
        t=np.arange(30)*5000
        raw=[((2**31-200+int(i)*100+2**31)%2**32)-2**31 for i in range(30)]
        result=derivatives(t,raw,5000)
        self.assertTrue(result)
        for r in result:
            self.assertAlmostEqual(r['position_rpm'],1/.005/6,places=8)
            self.assertAlmostEqual(r['position_alpha_rad_s2'],0,places=7)
            self.assertEqual(r['position_fit_span_ms'],20)

    def test_acceleration_units(self):
        t=np.arange(21)*5000;seconds=t/1e6
        result=derivatives(t,np.rint(.5*80000*seconds**2*100).astype(int),5000)
        self.assertTrue(result)
        for r in result:self.assertAlmostEqual(r['position_alpha_rad_s2'],math.radians(80000),places=6)

    def test_gap_not_bridged(self):
        t=np.array([0,5000,10000,15000,20000,60000,65000,70000,75000,80000])
        result=derivatives(t,np.rint(t*.01).astype(int),5000)
        self.assertTrue(result)
        self.assertTrue(all(x['position_fit_span_ms']==20 for x in result))

    def test_independent_position_discriminates_known_speed_lag(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'synthetic.csv';make_log(path)
            report=analyze([path]);p=report['pulse_summary'][0]
            self.assertEqual(p['status'],'position_comparison_available')
            self.assertEqual(p['position_to_rpm_sign'],1)
            self.assertAlmostEqual(p['descriptive_centroid_difference_ms'],80,delta=10)
            self.assertAlmostEqual(p['speed_area_to_position_area_ratio'],1,delta=.03)
            self.assertFalse(report['unique_delay_or_friction_model_identified'])
            self.assertFalse(report['body_work_measured'])
            self.assertFalse(report['absolute_inertia_identified'])
            self.assertGreater(p['conditional_delta_angular_momentum_nms'],0)

    def test_opposite_position_coordinate_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'reverse_coordinate.csv';make_log(path,position_sign=-1)
            p=analyze([path])['pulse_summary'][0]
            self.assertEqual(p['position_to_rpm_sign'],-1)
            self.assertAlmostEqual(p['descriptive_centroid_difference_ms'],80,delta=10)

    def test_ack_but_constant_position_is_not_motion_measurement(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'unsupported.csv';make_log(path,constant=True)
            report=analyze([path]);p=report['pulse_summary'][0]
            self.assertEqual(p['status'],'position_motion_not_observed')
            self.assertNotIn('conditional_delta_angular_momentum_nms',p)
            self.assertFalse(report['position_kinematics'])

    def test_partial_and_duplicate_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'stopped.csv';make_log(path,complete=False)
            self.assertEqual(analyze([path])['pulse_summary'][0]['status'],'run_or_pulse_incomplete')
            with self.assertRaisesRegex(ValueError,'duplicate'):analyze([path,path])


if __name__=='__main__':unittest.main()
