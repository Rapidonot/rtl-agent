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


def ui_result(script):
    source = PAGE.read_text()
    helpers = "\n".join(
        page_function(source, name)
        for name in ("validateWaveform", "waveformAttachment", "renderWaveformState")
    )
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
