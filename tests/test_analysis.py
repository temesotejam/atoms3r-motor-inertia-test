import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from analyze_response import analyze, fit_response, read_log


class AnalysisTests(unittest.TestCase):
    def test_known_response_with_friction_and_speed(self):
        rows = []
        a, b, c = 1600., -8., -.45
        for level in (50, 100, 150):
            for rep in range(3):
                for omega in (10., 20., 30.):
                    i = level/1000
                    rows.append(dict(current_A=i, omega_rad_s=omega, alpha_rad_s2=a*i+b+c*omega,
                                     command_abs_mA=level, run_id='x', pulse_id=f'{level}_{rep}'))
        result = fit_response(rows)
        self.assertAlmostEqual(result['current_coefficient_rad_s2_per_A'], a)
        self.assertAlmostEqual(result['offset_rad_s2'], b)
        self.assertAlmostEqual(result['speed_coefficient_per_s'], c)
        # A=(Kt/J) cannot distinguish these two physical parameter pairs.
        self.assertEqual(.048/3e-5, .096/6e-5)

    def test_confounded_current_and_speed_rejected(self):
        rows = [dict(current_A=level/1000, omega_rad_s=level, alpha_rad_s2=level*2,
                     command_abs_mA=level, run_id='x', pulse_id=f'{level}_{rep}')
                for level in (50,100,150) for rep in range(3)]
        with self.assertRaisesRegex(ValueError, 'cannot be separated'):
            fit_response(rows)

    def test_full_csv_and_no_unrequested_inertia(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'synthetic.csv'
            rows = []
            pid = 0
            # Integrate alpha = 1600 I - 5 - .3 omega; real time jitter and
            # separate current/speed acquisition clocks exercise conversion.
            for sign in (1,-1):
                for rep in range(3):
                    for level in (50,100,150):
                        pid += 1
                        base=pid*3000000; on=base+500000; zero=on+500000; off=zero+500
                        for index in range(140):
                            t_us=base+index*10000+(index%3)*70
                            speed_start=t_us+100; speed_end=t_us+500
                            t=((speed_start+speed_end)/2-on)/1e6
                            omega=(1600*level/1000-5)/.3*(1-np.exp(-.3*max(0,min(t,.5))))
                            amps=sign*level if 0 < (t_us+800-on)/1e6 < .5 else 0
                            rows.append(dict(sample=len(rows),t_us=t_us+1000,pulse_id=pid,
                                pulse_on_us=on,pulse_zero_us=zero,pulse_off_us=off,pulse_complete=1,pulse_cmd_mA=sign*level,
                                speed_valid=1,current_valid=1,status_valid=1,motor_error=0,
                                speed_start_us=speed_start,speed_end_us=speed_end,
                                current_start_us=t_us+600,current_end_us=t_us+1000,
                                rpm=sign*omega*60/(2*np.pi), actual_current_mA=amps,
                                voltage_mV=7400))
            with path.open('w',newline='',encoding='utf-8') as f:
                f.write(f'# schema=atoms3r_inertia_v1\n# configuration=current_wheel_and_weights\n# run_id=synthetic\n# result=done\n# pulse_width_ms=500\n# sample_count={len(rows)}\n')
                w=csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
            result, _, _ = analyze([path])
            self.assertFalse(result['absolute_inertia_identified'])
            self.assertNotIn('conditional_J_kg_m2', json.dumps(result))
            for model in result['models'].values():
                self.assertEqual(model['status'],'empirical_response_fitted')
                self.assertAlmostEqual(model['current_coefficient_rad_s2_per_A'],1600,delta=2)
            conditional, _, _ = analyze([path],kt=.048,kt_source='synthetic known motor')
            self.assertAlmostEqual(conditional['models']['positive']['conditional_J_kg_m2'],3e-5,delta=1e-7)
            with self.assertRaisesRegex(ValueError,'duplicate'):
                analyze([path,path])
            path.write_text(path.read_text().rsplit('\n',2)[0],encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'incomplete'):
                read_log(path)

    def test_kt_needs_source(self):
        with self.assertRaisesRegex(ValueError,'explicit'):
            analyze([],kt=.048)

if __name__=='__main__': unittest.main()
