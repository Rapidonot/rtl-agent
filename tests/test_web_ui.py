"""Exercise the actual page helpers without a server, model, or browser network."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="JavaScript UI checks require Node.js")
PAGE = Path(__file__).parents[1] / "web" / "index.html"


def page_function(source, name):
    start = source.index(f"function {name}(")
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    end = source.index("\n}", start) + 2
    return source[start:end]


def ui_result(script, names=("validateWaveform", "waveformAttachment", "renderWaveformState")):
    source = PAGE.read_text()
    helpers = "\n".join(page_function(source, name) for name in names)
    harness = """
const finite=(value,min=0,max=1e12)=>typeof value==='number'&&
  Number.isFinite(value)&&value>=min&&value<=max;
const nodes=new Map();
const $=id=>{
  if(!nodes.has(id))nodes.set(id,{hidden:false,textContent:'',title:'',attributes:{},
    setAttribute(k,v){this.attributes[k]=v;}});
  return nodes.get(id);
};
const view={hidden:false};
const document={querySelector:()=>view};
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", harness + helpers + "\n" + script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "reason",
    [
        "No waves.vcd file exists for this attempt.",
        "The VCD has no supported $timescale entry; its time unit is unknown.",
        "VCD timestamps move backwards; the trace cannot be plotted.",
    ],
)
def test_no_waveform_is_a_plain_message_with_real_tooltip_and_pass_unchanged(reason):
    result = ui_result(
        """
const attempt={status:'passed',gates:{simulate:'pass'},
  ...waveformAttachment({waveform:null,reason:REASON})};
const before=JSON.stringify(attempt);
renderWaveformState(attempt,false);
console.log(JSON.stringify({panelHidden:$('waveform-panel').hidden,
  messageHidden:$('no-waveform-message').hidden,text:$('waveform-absence-text').textContent,
  tooltip:$('waveform-reason').title,unchanged:before===JSON.stringify(attempt),
  controlsHidden:view.hidden}));
""".replace("REASON", json.dumps(reason))
    )
    assert result == {
        "panelHidden": True,
        "messageHidden": False,
        "text": "No waveform was recorded for this attempt",
        "tooltip": reason,
        "unchanged": True,
        "controlsHidden": True,
    }


def test_real_large_time_range_is_drawable_and_does_not_change_samples():
    result = ui_result("""
const wave={start_ns:0,duration_ns:1396000000000,
  channels:[{name:'clk',width:1,samples:[[0,0],[1396000000000,1]]}]};
const original=JSON.stringify(wave);
const attached=waveformAttachment({waveform:wave,reason:null});
renderWaveformState(attached,false);
console.log(JSON.stringify({hasWave:!!attached.waveform,panelHidden:$('waveform-panel').hidden,
  messageHidden:$('no-waveform-message').hidden,
  samplesUnchanged:JSON.stringify(attached.waveform)===original}));
""")
    assert result == {
        "hasWave": True,
        "panelHidden": False,
        "messageHidden": True,
        "samplesUnchanged": True,
    }


def test_rejected_waveform_is_a_display_reason_not_a_changed_verification_result():
    result = ui_result("""
const attempt={status:'passed',gates:{simulate:'pass'},
  ...waveformAttachment({start_ns:0,duration_ns:10,
    channels:[{name:'clk',width:1,samples:[[10,0],[0,1]]}]})};
renderWaveformState(attempt,false);
console.log(JSON.stringify({status:attempt.status,gate:attempt.gates.simulate,
  wave:attempt.waveform,tooltip:$('waveform-reason').title}));
""")
    assert result["status"] == "passed" and result["gate"] == "pass"
    assert result["wave"] is None and "timestamp order" in result["tooltip"]


def test_generated_runs_are_added_once_and_examples_are_separate():
    result = ui_result(
        """
const clone=structuredClone;
const state={library:[],serverRuns:[],sessionRuns:[]};
const CONFIG={records:[{id:'demo',category:'NPU',report:{run_id:'demo'}}]};
let renders=0;function renderGallery(){renders++;}
const report={source:'live',status:'running',run_id:'20261017T000000Z-12345678',
  title:'Custom chip block',category:'Custom',spec:'Design an 8-bit bit counter.',
  model:'fixture',attempts:[{number:1,status:'failed'}]};
rememberLiveRun(report);
const whileRunning=state.sessionRuns.length;
report.status='passed';report.attempts.push({number:2,status:'passed'});
const before=JSON.stringify(report);
rememberLiveRun(report);rememberLiveRun(report);
state.serverRuns=[{...liveGalleryRecord(report),title:'Server duplicate'}];
const records=allRecords();
console.log(JSON.stringify({whileRunning,yours:galleryRecords(records,'Your').length,
  examples:galleryRecords(records,'Examples').length,total:records.length,
  title:galleryRecords(records,'Your')[0].title,unchanged:JSON.stringify(report)===before}));
""",
        names=("allRecords", "galleryRecords", "liveGalleryRecord", "rememberLiveRun"),
    )
    assert result == {
        "whileRunning": 0,
        "yours": 1,
        "examples": 1,
        "total": 2,
        "title": "8-bit bit counter",
        "unchanged": True,
    }


def test_failed_live_runs_are_kept_and_replay_does_not_become_a_user_run():
    result = ui_result(
        """
const clone=structuredClone;
const base={status:'failed',run_id:'20261017T000000Z-12345678',title:'Counter',
  category:'Custom',model:'fixture',attempts:[{number:1,status:'failed'}]};
console.log(JSON.stringify({failed:liveGalleryRecord({...base,source:'live'}).report.status,
  replay:liveGalleryRecord({...base,source:'replay'})}));
""",
        names=("liveGalleryRecord",),
    )
    assert result == {"failed": "failed", "replay": None}


def test_first_visit_tour_can_be_skipped_and_reopened_without_changing_results():
    result = ui_result(
        """
const TOUR_KEY='rtl-agent.tour.v1';
const tourState={active:false,index:0,focus:null,page:null,scroll:0};
const state={running:false,page:'gallery',report:{status:'passed',attempts:[{passed:true}]}};
const before=JSON.stringify(state.report),callbacks=[],events={},saved=new Map();
const localStorage={getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)};
const scrollY=40;
function scrollTo(){}
function addEventListener(){}
function queueTourLayout(){}
class ResizeObserver{observe(){}}
function requestAnimationFrame(fn){callbacks.push(fn);}
function setPage(page){state.page=page;}
function showTourStep(index){tourState.index=index;}
document.querySelector=()=>null;
document.addEventListener=(name,handler)=>events[name]=handler;
document.activeElement={tagName:'BODY'};
for(const id of ['open-tour','tour-skip','tour-back','tour-next']){
  $(id).handlers={};$(id).addEventListener=(event,handler)=>$(id).handlers[event]=handler;
  $(id).focus=()=>{};
}
initializeTour();while(callbacks.length)callbacks.shift()();
const firstVisit=tourState.active&&$('viewport').inert&&state.page==='workbench';
$('tour-skip').handlers.click();
const skipped=!tourState.active&&!$('viewport').inert&&state.page==='gallery';
initializeTour();const secondVisitScheduled=callbacks.length;
$('open-tour').handlers.click();const reopened=tourState.active;
events.keydown({key:'Escape',preventDefault(){},stopImmediatePropagation(){}});
console.log(JSON.stringify({firstVisit,skipped,secondVisitScheduled,reopened,
  escapeCloses:!tourState.active,seen:saved.get(TOUR_KEY),unchanged:JSON.stringify(state.report)===before}));
""",
        names=("startTour", "closeTour", "initializeTour"),
    )
    assert result == {
        "firstVisit": True,
        "skipped": True,
        "secondVisitScheduled": 0,
        "reopened": True,
        "escapeCloses": True,
        "seen": "seen",
        "unchanged": True,
    }


def test_hosted_requests_send_session_header_to_backend_without_cookies_or_key():
    result = ui_result(
        """
const API_BASE_URL='https://backend.hf.space';
let liveSessionToken='fixture-session-token',apiHealth={session:{remaining:9}};
let request;
async function fetch(url,options){request={url,method:options.method,
  access:options.headers.get('Authorization'),credentials:options.credentials,
  body:options.body};return {status:202,ok:true,json:async()=>({run_id:'fixture'})};}
await apiRequest('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({spec:'An adder.',top:'dut',max_attempts:2})});
console.log(JSON.stringify(request));
""",
        names=("apiRequest",),
    )
    assert result == {
        "url": "https://backend.hf.space/api/run",
        "method": "POST",
        "access": "Bearer fixture-session-token",
        "credentials": "omit",
        "body": '{"spec":"An adder.","top":"dut","max_attempts":2}',
    }


def test_old_unauthorized_response_cannot_clear_a_new_session():
    result = ui_result(
        """
const API_BASE_URL='https://backend.hf.space';
const LIVE_SESSION_KEY='fixture-session';
const sessionStorage={removeItem(){}};
let liveSessionToken='old-session',apiHealth={session:{remaining:9}},deliver;
function fetch(){return new Promise(resolve=>deliver=resolve);}
const pending=apiRequest('/api/health');
liveSessionToken='new-session';
deliver({status:401,ok:false,json:async()=>({detail:'Access locked.'})});
try{await pending;}catch{}
console.log(JSON.stringify({session:liveSessionToken,remaining:apiHealth.session.remaining}));
""",
        names=("apiRequest", "clearLiveSession"),
    )
    assert result == {"session": "new-session", "remaining": 9}


def test_hosted_health_unauthorized_without_a_token_does_not_retry_forever():
    result = ui_result(
        """
const API_BASE_URL='https://backend.hf.space';
let liveSessionToken='',apiHealth=null,healthRequest=0,calls=0,message='';
async function apiRequest(){calls++;throw Object.assign(new Error('Unavailable'),{status:401});}
function showConnectionError(text){message=text;}
const ready=await refreshHealth();
console.log(JSON.stringify({calls,ready,disabled:$('mode-live').disabled,message}));
""",
        names=("refreshHealth",),
    )
    assert result["calls"] == 1 and result["ready"] is None
    assert result["disabled"] and "unavailable" in result["message"]


def test_shared_daily_count_disables_live_and_reset_restores_it_without_changing_results():
    result = ui_result(
        """
const state={running:false,report:{status:'passed',attempts:[{status:'passed'}]}},
  engine={mode:'live'};
let liveSessionToken='server-issued-token',sessionRequest=null;
const apiHealth={hosted:true,live_available:true,session:{runs:40},
  usage:{remaining:0,limit:20}};
const before=JSON.stringify(state.report);
updateHealthDisplay();
const exhausted={disabled:$('run').disabled,label:$('live-status').textContent};
engine.mode='replay';updateHealthDisplay();const replayAvailable=!$('run').disabled;
engine.mode='live';apiHealth.usage.remaining=20;updateHealthDisplay();
console.log(JSON.stringify({exhausted,replayAvailable,resetEnablesLive:!$('run').disabled,
  resetLabel:$('live-status').textContent,unchanged:before===JSON.stringify(state.report)}));
""",
        names=("hasLiveSession", "liveQuotaReached", "setLimit", "updateHealthDisplay"),
    )
    assert result["exhausted"]["disabled"]
    assert "0 of 20, shared by everyone" in result["exhausted"]["label"]
    assert "midnight Singapore time" in result["exhausted"]["label"]
    assert "20 of 20" in result["resetLabel"]
    assert result["replayAvailable"] and result["resetEnablesLive"] and result["unchanged"]


def test_live_attempt_control_increases_past_three_and_respects_server_bounds():
    result = ui_result(
        """
const engine={mode:'live'};
const apiHealth={hosted:true,limits:{max_attempts:5}};
function select(value){
  setLimit(value);
  return {value:Number($('max-attempts').value),max:Number($('max-attempts').max),
    minusDisabled:$('decrement').disabled,plusDisabled:$('increment').disabled};
}
const three=select(3),four=select(three.value+1),five=select(four.value+1);
const over=select(99),one=select(0);
apiHealth.limits.max_attempts=2;const configured=select(5);
engine.mode='replay';const replay=select(5);
console.log(JSON.stringify({three,four,five,over,one,configured,replay}));
""",
        names=("setLimit",),
    )
    assert [result[key]["value"] for key in ("three", "four", "five")] == [3, 4, 5]
    assert not result["three"]["plusDisabled"] and not result["four"]["plusDisabled"]
    assert result["five"]["plusDisabled"] and result["over"]["value"] == 5
    assert result["one"]["minusDisabled"] and result["one"]["value"] == 1
    assert result["configured"]["max"] == result["configured"]["value"] == 2
    assert result["configured"]["plusDisabled"]
    assert result["replay"]["max"] == result["replay"]["value"] == 5
