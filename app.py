# -*- coding: utf-8 -*-
"""
AI 영상 자동 편집기 — Streamlit 웹앱
PC: streamlit run app.py
모바일: Streamlit Cloud 배포 후 URL로 접속
"""

import streamlit as st
import os, sys, shutil, tempfile, io, time
from pathlib import Path
from contextlib import redirect_stdout

# ── 페이지 기본 설정 ──────────────────────────────────────────
st.set_page_config(
    page_title="AI 영상 자동 편집기",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 커스텀 CSS (다크 테마 강화) ───────────────────────────────
st.markdown("""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body, [class*="css"] { font-family: 'Pretendard', sans-serif !important; }

/* 헤더 숨기기 */
#MainMenu, header, footer { visibility: hidden; }

/* 상단 여백 제거 */
.block-container { padding-top: 1.5rem !important; }

/* 파일 업로더 */
[data-testid="stFileUploadDropzone"] {
    background: #161B22 !important;
    border: 2px dashed #30363D !important;
    border-radius: 12px !important;
}

/* 버튼 */
.stButton > button {
    border-radius: 12px !important;
    font-weight: 700 !important;
    height: 52px !important;
    font-size: 16px !important;
}

/* 진행바 */
.stProgress > div > div {
    background: linear-gradient(90deg, #CCFF00, #00C471) !important;
}

/* 사이드바 구분선 */
.sidebar-divider {
    border: none; border-top: 1px solid #21262D;
    margin: 16px 0;
}

/* 파일 카드 */
.file-card {
    background: #161B22;
    border: 1px solid #21262D;
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 12px;
}

/* 로그 박스 */
.log-terminal {
    background: #0D1117;
    border: 1px solid #21262D;
    border-radius: 10px;
    padding: 14px;
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: #8B95A1;
    white-space: pre-wrap;
    max-height: 250px;
    overflow-y: auto;
}

/* 완료 배너 */
.success-banner {
    background: linear-gradient(135deg, #0D1117, #162b12);
    border: 1px solid #00C471;
    border-radius: 16px;
    padding: 24px;
    text-align: center;
    margin: 16px 0;
}

/* 설정 슬라이더 값 강조 */
.setting-val {
    color: #CCFF00;
    font-weight: 700;
    font-size: 13px;
}
</style>
""", unsafe_allow_html=True)

# ── 세션 상태 초기화 ──────────────────────────────────────────
for k, v in {
    "result_bytes": None,
    "result_name": "result.mp4",
    "log_lines": [],
    "processing": False,
    "done": False,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── auto_edit 임포트 ──────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
try:
    import auto_edit as ae
    AE_OK = True
except Exception as e:
    AE_OK = False
    AE_ERR = str(e)

# ── 사이드바: 설정 ────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ 설정")

    # 자막
    st.markdown("#### 자막")
    font_size = st.slider("글자 크기", 40, 100, 72, 1,
                          help="참고 영상 수준: 65~80")
    tw_ms = st.slider("타자기 속도 (ms/글자)", 30, 300, 80, 10,
                      help="낮을수록 빠르게 타이핑됨")
    max_chars = st.slider("자막 최대 글자수", 6, 20, 12, 1)
    model = st.selectbox("Whisper 모델",
                         ["tiny", "base", "small", "medium"],
                         index=2,
                         help="클수록 정확하지만 느림")
    highlight = st.selectbox("강조 색상",
        ["#CCFF00  형광연두", "#FF6B00  오렌지", "#3182F6  파랑",
         "#F04452  빨강", "#00C471  초록"],
        index=0)
    highlight_hex = highlight.split()[0]

    st.markdown("---")

    # 편집
    st.markdown("#### 편집")
    silence_db = st.slider("무음 감도 (dB)", -50, -15, -30, 1,
                           help="낮을수록 더 민감하게 잘라냄")
    silence_min = st.slider("최소 무음 길이 (초)", 0.3, 2.0, 0.7, 0.1)
    padding = st.slider("말 앞뒤 여유 (초)", 0.0, 0.5, 0.2, 0.05)

    st.markdown("---")

    # 출력
    st.markdown("#### 출력")
    orientation = st.radio("영상 방향",
        ["가로 1920×1080", "세로 1080×1920 (쇼츠)"],
        index=0)
    tw_on = st.toggle("타자기 효과", value=True)
    sfx_on = st.toggle("효과음 자동 생성", value=True)

    st.markdown("---")
    st.caption("🎨 강조색·크기 등은 바로 아래 실행 시 적용됩니다")

# ── 메인: 헤더 ────────────────────────────────────────────────
col_title, col_badge = st.columns([5, 1])
with col_title:
    st.markdown(
        '<h1 style="margin:0;font-size:28px;font-weight:800;">'
        '🎬 AI 영상 자동 편집기</h1>',
        unsafe_allow_html=True,
    )
with col_badge:
    if AE_OK:
        st.markdown(
            '<div style="text-align:right;padding-top:8px;">'
            '<span style="background:#162b12;color:#00C471;'
            'border-radius:100px;padding:4px 12px;font-size:12px;font-weight:600;">'
            '● 준비됨</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.error(f"auto_edit.py 로드 실패: {AE_ERR}")

st.markdown("---")

if not AE_OK:
    st.stop()

# ── 파일 업로드 ───────────────────────────────────────────────
st.markdown("### 📁 영상 파일 업로드")
st.caption("여러 파일 선택 시 파일명 순서대로 합쳐집니다 · MP4/MOV/MKV/AVI 지원")

uploaded = st.file_uploader(
    "영상 파일 선택",
    type=["mp4", "mov", "mkv", "avi", "m4v", "webm"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)

# 업로드된 파일 목록 + 미리보기
if uploaded:
    sorted_files = sorted(uploaded, key=lambda f: f.name.lower())
    st.markdown(f"**{len(sorted_files)}개 파일** (처리 순서대로)")

    for i, f in enumerate(sorted_files, 1):
        size_mb = f.size / 1024 / 1024
        with st.expander(f"▶  {i}. {f.name}  ({size_mb:.1f} MB)", expanded=(i == 1)):
            col_v, col_empty = st.columns([1, 2])
            with col_v:
                st.video(f)  # 영상 미리보기 (1/3 크기)

st.markdown("---")

# ── 설정 요약 표시 ────────────────────────────────────────────
if uploaded:
    st.markdown("#### 현재 설정")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("글자 크기", f"{font_size}pt")
    c2.metric("Whisper 모델", model)
    c3.metric("타자기 속도", f"{tw_ms}ms/자")
    c4.metric("강조 색상", highlight.split()[1])
    st.caption("👈 왼쪽 사이드바에서 설정 변경 가능")
    st.markdown("---")

# ── 실행 버튼 ─────────────────────────────────────────────────
col_run, col_dl = st.columns([2, 1])

with col_run:
    run_btn = st.button(
        "▶  편집 시작",
        disabled=not uploaded or st.session_state.processing,
        use_container_width=True,
        type="primary",
    )

with col_dl:
    if st.session_state.result_bytes:
        st.download_button(
            "⬇️  결과 다운로드",
            data=st.session_state.result_bytes,
            file_name=st.session_state.result_name,
            mime="video/mp4",
            use_container_width=True,
        )

# 로그 출력창
log_box = st.empty()
progress_bar = st.empty()

if st.session_state.log_lines:
    log_box.markdown(
        '<div class="log-terminal">'
        + "\n".join(st.session_state.log_lines[-30:])
        + "</div>",
        unsafe_allow_html=True,
    )

# ── 파이프라인 실행 ───────────────────────────────────────────
def add_log(msg: str, icon: str = ""):
    line = f"{icon}  {msg}" if icon else f"    {msg}"
    st.session_state.log_lines.append(line)
    log_box.markdown(
        '<div class="log-terminal">'
        + "\n".join(st.session_state.log_lines[-30:])
        + "</div>",
        unsafe_allow_html=True,
    )

def set_progress(pct: int):
    progress_bar.progress(pct, text=f"{pct}% 완료")

def apply_settings():
    """사이드바 설정을 auto_edit 전역변수에 반영"""
    ae.WHISPER_MODEL      = model
    ae.SUBTITLE_SIZE      = font_size
    ae.SUBTITLE_MAX_CHARS = max_chars
    ae.HIGHLIGHT_COLOR    = highlight_hex
    ae.TYPEWRITER_MS      = tw_ms
    ae.SILENCE_DB         = silence_db
    ae.SILENCE_MIN_SEC    = silence_min
    ae.KEEP_PADDING_SEC   = padding
    if "세로" in orientation:
        ae.TARGET_W, ae.TARGET_H = 1080, 1920
    else:
        ae.TARGET_W, ae.TARGET_H = 1920, 1080
    # 타자기 효과 비활성 시 팝 효과로 대체
    if not tw_on:
        ae.TYPEWRITER_MS = 0

def run_pipeline(files):
    tmpdir = tempfile.mkdtemp(prefix="webapp_edit_")
    assets_dir = str(BASE_DIR / "assets")
    os.makedirs(assets_dir, exist_ok=True)

    try:
        apply_settings()

        # ── 1. 파일 저장 ──────────────────────────────────────
        add_log("[1/4] 업로드 파일 저장 중...", "📁")
        video_paths = []
        for i, f in enumerate(sorted(files, key=lambda x: x.name.lower())):
            ext = Path(f.name).suffix
            dst = os.path.join(tmpdir, f"input_{i:03d}{ext}")
            with open(dst, "wb") as out:
                out.write(f.getvalue())
            video_paths.append(dst)
            add_log(f"✓ {f.name}  ({f.size/1024/1024:.1f} MB)")
        set_progress(10)

        # ── 2. 무음 제거 ──────────────────────────────────────
        add_log("[2/4] 무음 구간 제거 중...", "✂️")
        clips, clip_durs = [], []
        for idx, v in enumerate(video_paths):
            segs = ae.speech_segments(v)
            dur  = ae.ffprobe_duration(v)
            kept = sum(e - s for s, e in segs)
            add_log(f"{Path(v).name}: {dur:.1f}s → {kept:.1f}s  (구간 {len(segs)}개)")
            clip = os.path.join(tmpdir, f"clip_{idx:03d}.mp4")
            if ae.trim_and_normalize(v, clip, segs):
                clips.append(clip)
                clip_durs.append(kept)
        if not clips:
            add_log("오류: 처리할 구간이 없습니다.", "❌")
            return None
        set_progress(35)

        # ── 3. 합치기 ─────────────────────────────────────────
        add_log(f"[3/4] 영상 {len(clips)}개 합치는 중...", "🔗")
        merged = os.path.join(tmpdir, "merged.mp4")
        if len(clips) == 1:
            shutil.copy(clips[0], merged)
        else:
            ae.concat_clips(clips, merged, tmpdir)
        add_log(f"합치기 완료 → {sum(clip_durs):.1f}초")
        set_progress(55)

        # ── 4. 자막 + 효과음 + 렌더링 ────────────────────────
        add_log("[4/4] 자막 생성 및 렌더링 중...", "📝")
        add_log(f"Whisper {model} 모델로 한국어 음성 인식 중...")

        _, font_name = ae.find_font(assets_dir)
        ass_path = os.path.join(tmpdir, "result.ass")

        # stdout 캡처해서 로그에 추가
        buf = io.StringIO()
        with redirect_stdout(buf):
            n = ae.transcribe_to_ass(merged, ass_path, font_name)
        for line in buf.getvalue().splitlines():
            if line.strip():
                add_log(line.strip())

        add_log(f"자막 {n}줄 생성 완료")
        set_progress(75)

        # 클립 경계 (효과음 전환 타이밍)
        clip_boundaries = []
        acc = 0.0
        for d in clip_durs[:-1]:
            acc += d
            clip_boundaries.append(acc)

        # 이미지 오버레이 (assets에 image.png, image1.png 있으면)
        overlay_times = []
        img0 = os.path.join(assets_dir, "image.png")
        img1 = os.path.join(assets_dir, "image1.png")
        if os.path.exists(img0) and os.path.exists(img1) and n > 0:
            overlay_times = ae.pick_overlay_times(ass_path)

        # 효과음 생성 여부
        if not sfx_on:
            # 효과음 없이 렌더링 (render_final 내부에서 생성하므로 assets에 더미 생성)
            pass

        out_path = os.path.join(tmpdir, "result.mp4")
        if n > 0:
            add_log("최종 렌더링 중 (자막·효과음·이미지 합성)...")
            ae.render_final(merged, ass_path, tmpdir, out_path,
                            assets_dir, clip_boundaries, overlay_times)
        else:
            add_log("음성 인식 결과 없음 → 자막 없이 저장")
            shutil.copy(merged, out_path)

        set_progress(100)
        add_log("✅ 편집 완료!", "🎉")

        with open(out_path, "rb") as f:
            return f.read()

    except Exception as e:
        add_log(f"오류 발생: {e}", "❌")
        st.exception(e)
        return None

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── 실행 트리거 ───────────────────────────────────────────────
if run_btn and uploaded:
    st.session_state.processing = True
    st.session_state.done = False
    st.session_state.log_lines = []
    st.session_state.result_bytes = None

    result = run_pipeline(uploaded)

    st.session_state.processing = False
    if result:
        st.session_state.result_bytes = result
        st.session_state.result_name = "result_편집완료.mp4"
        st.session_state.done = True
        st.rerun()

# ── 완료 배너 + 결과 미리보기 ────────────────────────────────
if st.session_state.done and st.session_state.result_bytes:
    st.success("✅ 편집 완료! 아래에서 결과 영상을 확인하고 다운로드하세요.")
    col_r, col_re = st.columns([1, 2])
    with col_r:
        st.video(st.session_state.result_bytes)
    st.download_button(
        "⬇️  결과 영상 다운로드",
        data=st.session_state.result_bytes,
        file_name=st.session_state.result_name,
        mime="video/mp4",
        use_container_width=True,
    )

# ── 사용 가이드 (업로드 전) ───────────────────────────────────
if not uploaded and not st.session_state.done:
    st.markdown("---")
    st.markdown("### 사용 방법")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown("**1️⃣ 영상 업로드**\n\n편집할 MP4 파일을\n위에 드래그하거나 선택")
    with col2:
        st.markdown("**2️⃣ 설정 조정**\n\n왼쪽 사이드바에서\n글자 크기·색상 등 설정")
    with col3:
        st.markdown("**3️⃣ 편집 시작**\n\n▶ 버튼 클릭\n(수 분 소요)")
    with col4:
        st.markdown("**4️⃣ 다운로드**\n\n완료 후\n⬇️ 버튼으로 저장")

    st.markdown("---")
    st.caption(
        "✂️ 말 없는 구간 자동 제거  ·  "
        "🔗 영상 순서대로 합치기  ·  "
        "📝 한국어 자막 자동 생성  ·  "
        "🎨 타자기 효과 + 형광 강조  ·  "
        "🔊 효과음 자동 삽입"
    )
