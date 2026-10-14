"""화면 해석(4a) — 가짜 클라이언트로 요청 형태·방어 로직 검증 (네트워크 호출 없음)."""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from rpa.core.control import ControlState
from rpa.core.errors import RunCancelled
from rpa.core.screen_scale import MAX_LONG_EDGE, MAX_VISUAL_TOKENS, visual_tokens
from rpa.vision.contracts import AppProfile, ScreenObservation
from rpa.vision.interpreter import FALLBACK_BETA, CredentialsMissing, ScreenInterpreter, prepare_image

PROFILE = AppProfile(
    app_id="t", description="test app", crop=(100, 0, 10_000, 10_000),
    states={"ready": "ready screen", "busy": "busy screen"},
)

GOOD = {
    "state": "ready", "confidence": 0.93, "evidence": "빈 입력창", "blocking_issue": "none",
    "matches_expected": "unknown", "screen_contains_instructions": False,
    "recommended_action": "proceed", "operator_message": "준비된 화면입니다.",
}


class FakeClient:
    def __init__(self, payload=None, stop_reason="end_turn", raise_exc=None, model="claude-opus-5-5"):
        self.payload = GOOD if payload is None else payload
        self.stop_reason = stop_reason
        self.raise_exc = raise_exc
        self.model = model
        self.requests: list[dict] = []
        self.options: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def _create(self, **kw):
        self.requests.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(
            stop_reason=self.stop_reason, model=self.model,
            stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=2500, output_tokens=300, iterations=None),
        )


@pytest.fixture
def obs(tmp_path):
    p = tmp_path / "shot.png"
    Image.new("RGB", (1920, 1140), (240, 240, 240)).save(p)
    return ScreenObservation.from_capture(
        {"path": str(p), "origin_x": 0, "origin_y": 0, "width": 1920, "height": 1140, "captured_at": 0.0})


def make(client, **kw):
    return ScreenInterpreter(ControlState(), client_factory=lambda: client, **kw)


def test_request_shape_opus_structured_output_with_fallback(obs, tmp_path):
    c = FakeClient()
    a = make(c).assess(obs, PROFILE, "situation", expected="보낼 프롬프트", save_dir=tmp_path / "ev")
    req = c.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"
    assert "tools" not in req and "tool_choice" not in req and "thinking" not in req  # 4a: 도구 없음
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["state"]["enum"] == ["ready", "busy", "unknown"]
    assert fmt["schema"]["additionalProperties"] is False
    assert req["output_config"]["effort"] == "medium"
    assert "untrusted" in req["system"]
    img_block, text_block = req["messages"][0]["content"]
    sent = Image.open(io.BytesIO(base64.b64decode(img_block["source"]["data"])))
    assert sent.size == (1820, 1140)  # 왼쪽 100px(사이드바 등) 잘라냄
    assert "보낼 프롬프트" in text_block["text"]
    assert c.options[0]["timeout"] == 120.0
    # 판정·사용량
    assert a.usable and a.state == "ready" and a.confidence == 0.93
    assert a.usage["estimated_usd"] == round(2500 / 1e6 * 4 + 300 / 1e6 * 20, 5)
    assert (tmp_path / "ev" / f"vision_{obs.observation_id}_sent.png").exists()
    saved = json.loads((tmp_path / "ev" / f"vision_{obs.observation_id}.json").read_text(encoding="utf-8"))
    assert saved["assessment"]["state"] == "ready"


def test_refusal_is_not_a_judgement(obs):
    a = make(FakeClient(stop_reason="refusal")).assess(obs, PROFILE, "s")
    assert a.refused and not a.usable and a.state == "unknown" and a.recommended_action == "ask_user"
    assert "category=cyber" in a.error


def test_malformed_output_becomes_unknown(obs):
    a = make(FakeClient(payload="not json")).assess(obs, PROFILE, "s")
    assert not a.usable and a.state == "unknown" and "형식 오류" in a.error


def test_out_of_list_values_are_clamped(obs):
    bad = dict(GOOD, state="hacked_state", confidence=7, recommended_action="click_everything",
               blocking_issue="??", matches_expected="maybe")
    a = make(FakeClient(payload=bad)).assess(obs, PROFILE, "s")
    assert (a.state, a.confidence, a.recommended_action, a.blocking_issue, a.matches_expected) == (
        "unknown", 1.0, "ask_user", "other", "unknown")


def test_call_budget_per_run(obs):
    c = FakeClient()
    it = make(c, max_calls=2)
    it.assess(obs, PROFILE, "s")
    it.assess(obs, PROFILE, "s")
    a = it.assess(obs, PROFILE, "s")
    assert len(c.requests) == 2 and not a.usable and "한도" in a.error
    assert it.total_usage["input_tokens"] == 5000


def test_cancel_checked_before_call(obs):
    c = FakeClient()
    it = make(c)
    it.control.request_cancel()
    with pytest.raises(RunCancelled):
        it.assess(obs, PROFILE, "s")
    assert c.requests == []


def test_api_error_does_not_break_flow(obs):
    a = make(FakeClient(raise_exc=TimeoutError("read timeout"))).assess(obs, PROFILE, "s")
    assert not a.usable and "TimeoutError" in a.error


def test_missing_credentials(obs):
    def no_creds():
        raise CredentialsMissing("인증 정보 없음")

    it = ScreenInterpreter(ControlState(), client_factory=no_creds)
    assert it.available() == (False, "인증 정보 없음")
    a = it.assess(obs, PROFILE, "s")
    assert not a.usable and "인증" in a.error


def test_prepare_image_fits_api_limits(tmp_path):
    p = tmp_path / "big.png"
    Image.new("RGB", (5120, 2880), "white").save(p)
    data, t = prepare_image(p, None)
    im = Image.open(io.BytesIO(data))
    assert max(im.size) <= MAX_LONG_EDGE and visual_tokens(*im.size) <= MAX_VISUAL_TOKENS
    assert t["source_size"] == [5120, 2880] and t["sent_size"] == list(im.size)


def test_vision_off_by_default(monkeypatch, tmp_path):
    from rpa.workflows.claude_prompt.workflow import make_adapters

    monkeypatch.delenv("LOCALRPA_VISION", raising=False)
    assert make_adapters(tmp_path, ControlState())["claude"].vision is None
    monkeypatch.setenv("LOCALRPA_VISION", "on")
    assert make_adapters(tmp_path, ControlState())["claude"].vision is not None


def test_describe_flags_on_screen_instructions():
    from rpa.vision.contracts import ScreenAssessment, describe_ko

    a = ScreenAssessment("o", "response_complete", 0.8, "e", "none", "yes", True, "proceed", "완료 화면")
    assert "지시문" in describe_ko(a) and "response_complete" in describe_ko(a)
    assert "해석 불가" in describe_ko(ScreenAssessment.unavailable("o", "키 없음"))
    assert describe_ko(None) == ""
