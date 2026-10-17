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
