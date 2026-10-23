import copy,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from morla_piano.engine import Engine,FakeBackend,VirtualClock
from morla_piano.model import load_config,plan_score,validate_config,performance_timing
PROFILE=dict(sdk_speed_percent=10,max_arm_velocity_rad_s=.225,move_duration_s=.45,settle_s=.2,release_settle_s=.12,min_press_s=.15)
ROOT=Path(__file__).resolve().parents[1]
class SpeedBackend(FakeBackend):
 def __init__(self,*args):super().__init__(*args);self.speeds=[]
 def set_arm_speed(self,p,targets,check):check();self.speeds.append(p)
def test_prepare_and_completed_play_restore_base_speed():
 c=load_config(ROOT/'config/piano.placeholder.json');c['performance']=PROFILE.copy();clock=VirtualClock();b=SpeedBackend(c,clock);e=Engine(c,b,clock)
 e.prepare();assert b.speeds==[None];e.play(plan_score(ROOT/'scores/demo.json',c));assert e.state=='FINISHED';assert b.speeds==[None,10,None];assert c['timing']['max_arm_velocity_rad_s']==.15
 assert not e.pressed

def test_cancel_restores_speed_without_estop():
 c=load_config(ROOT/'config/piano.placeholder.json');c['performance']=PROFILE.copy();clock=VirtualClock();b=SpeedBackend(c,clock);e=Engine(c,b,clock);e.prepare()
 original=e._send
 def cancel():
  original()
  if e.state=='PLAYING' and e.clock.now()>6:e.request_stop()
 e._send=cancel;e.play(plan_score(ROOT/'scores/demo.json',c))
 assert e.state=='STOPPED' and not b.stopped and b.speeds[-1] is None

@pytest.mark.parametrize('key,value',[('sdk_speed_percent',True),('sdk_speed_percent',10.5),('sdk_speed_percent',21),('settle_s',.08),('max_arm_velocity_rad_s',float('nan'))])
def test_bad_profile_rejected(key,value):
 c=load_config(ROOT/'config/piano.placeholder.json');c['performance']=PROFILE.copy();c['performance'][key]=value
 with pytest.raises(ValueError):validate_config(c)
