"""Claude 데스크톱 앱 화면 상태 정의 (화면 해석 4a용).

사이드바(대화 목록)는 업무 판정에 필요 없고 다른 대화 제목이 보이므로 잘라내고 보낸다.
좌표는 125% 배율·1920px 폭 창의 클라이언트 기준 (selectors.json 환경과 동일).
"""

from rpa.vision.contracts import AppProfile

SIDEBAR_WIDTH = 360

CLAUDE_PROFILE = AppProfile(
    app_id="claude_desktop",
    description="Claude desktop app (Korean UI), main conversation area only (left sidebar cropped out)",
    crop=(SIDEBAR_WIDTH, 0, 100_000, 100_000),
    states={
        "new_chat_empty": "New-chat start screen: centered greeting, EMPTY input box with placeholder "
                          "'오늘 어떤 도움을 드릴까요?', mode toggle '채팅 | Cowork' below it, no message bubbles.",
        "prompt_typed_not_sent": "New-chat start screen (centered input, no message bubbles) but the input box "
                                 "already contains typed text that has not been sent.",
        "generating": "A user message bubble is shown and the assistant reply is still being produced "
                      "(progress dots/spinner where the reply goes, or a stop button in the input box).",
        "response_complete": "A user message bubble and a finished assistant reply are shown, with the icon row "
                             "under the reply (copy, read aloud, thumbs up, thumbs down, retry).",
        "error_or_limit": "An error, usage-limit, rate-limit or network-problem message is shown in the "
                          "conversation area instead of (or after) the reply.",
        "login_or_onboarding": "Sign-in, sign-up, account selection or onboarding screen.",
        "modal_dialog": "A dialog, menu or pop-up is open on top of the app content.",
        "loading": "The app or page is still loading (blank or skeleton content).",
        "other_screen": "Some other app screen (settings, projects list, artifacts, etc.), not a conversation.",
    },
    extra_guidance=(
        "- In the new-chat screen the selected mode in the '채팅 | Cowork' toggle has a white highlighted pill; "
        "mention in evidence which one is selected.\n"
        "- For matches_expected, compare the most recent USER message bubble (right side) with the expected "
        "prompt; ignore assistant text.\n"
        "- The automation sends exactly one prompt per new chat; an older multi-turn conversation means the "
        "wrong conversation is open (matches_expected = no)."
    ),
)
