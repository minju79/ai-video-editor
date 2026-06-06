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
    # 2단계 처리용
    "stage": "upload",          # "upload" | "edit_sub" | "done"
    "subtitle_chunks": [],      # [(ts, te, text), ...]
    "merged_tmpdir": None,      # 1단계 tmpdir (2단계까지 유지)
    "merged_path": None,        # merged.mp4 경로
    "clip_boundaries": [],
    "overlay_times": [],
    "font_name": "NanumGothic",
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
    subtitle_style = st.radio(
        "자막 스타일",
        ["🎨 Modern (형광 강조 + 타자기)", "📺 예능 (하늘색 박스)"],
        index=0,
    )
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

    # BGM
    st.markdown("#### 🎵 배경음악 (BGM)")
    bgm_file = st.file_uploader(
        "MP3 또는 WAV 파일 업로드",
        type=["mp3", "wav", "m4a"],
        label_visibility="collapsed",
    )
    if bgm_file:
        st.audio(bgm_file)
        bgm_vol = st.slider("BGM 볼륨", 1, 40, 12, 1,
                            help="12% 권장 (목소리가 잘 들리게)")
    else:
        bgm_vol = 12
        st.caption("assets/bgm.mp3 파일이 있으면 자동 적용됩니다")

    st.markdown("---")
    st.caption("🎨 설정은 편집 시작 시 적용됩니다")

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
    # 자막 스타일
    ae.SUBTITLE_STYLE = "variety" if "예능" in subtitle_style else "modern"
    # 타자기 효과 비활성 시
    if not tw_on:
        ae.TYPEWRITER_MS = 0

def run_stage1(files):
    """1단계: 무음 제거 + 합치기 + 자막 생성 → 자막 수정 대기"""
    assets_dir = str(BASE_DIR / "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # 이전 tmpdir 정리
    if st.session_state.merged_tmpdir:
        shutil.rmtree(st.session_state.merged_tmpdir, ignore_errors=True)

    tmpdir = tempfile.mkdtemp(prefix="webapp_s1_")
    try:
        apply_settings()

        add_log("[1/3] 파일 저장 중...", "📁")
        video_paths = []
        for i, f in enumerate(sorted(files, key=lambda x: x.name.lower())):
            ext = Path(f.name).suffix
            dst = os.path.join(tmpdir, f"input_{i:03d}{ext}")
            with open(dst, "wb") as out:
                out.write(f.getvalue())
            video_paths.append(dst)
            add_log(f"✓ {f.name}  ({f.size/1024/1024:.1f} MB)")
        set_progress(15)

        add_log("[2/3] 무음 구간 제거 + 합치는 중...", "✂️")
        clips, clip_durs = [], []
        for idx, v in enumerate(video_paths):
            segs = ae.speech_segments(v)
            dur  = ae.ffprobe_duration(v)
            kept = sum(e - s for s, e in segs)
            add_log(f"{Path(v).name}: {dur:.1f}s → {kept:.1f}s")
            clip = os.path.join(tmpdir, f"clip_{idx:03d}.mp4")
            if ae.trim_and_normalize(v, clip, segs):
                clips.append(clip)
                clip_durs.append(kept)
        if not clips:
            add_log("오류: 처리할 구간 없음", "❌")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False

        merged = os.path.join(tmpdir, "merged.mp4")
        if len(clips) == 1:
            shutil.copy(clips[0], merged)
        else:
            ae.concat_clips(clips, merged, tmpdir)
        set_progress(50)

        add_log("[3/3] 한국어 음성 인식 중...", "📝")
        add_log(f"Whisper {model} 모델 사용 중 (시간이 걸릴 수 있습니다)")
        _, font_name = ae.find_font(assets_dir)

        buf = io.StringIO()
        with redirect_stdout(buf):
            chunks = ae.transcribe_to_chunks(merged)
        add_log(f"자막 {len(chunks)}줄 인식 완료 → 수정 후 영상 완성하세요!")
        set_progress(100)

        # 클립 경계
        clip_boundaries = []
        acc = 0.0
        for d in clip_durs[:-1]:
            acc += d
            clip_boundaries.append(acc)

        # 이미지 오버레이 타이밍
        overlay_times = []
        img0 = os.path.join(assets_dir, "image.png")
        img1 = os.path.join(assets_dir, "image1.png")
        if os.path.exists(img0) and os.path.exists(img1) and chunks:
            tmp_ass = os.path.join(tmpdir, "tmp.ass")
            content = ae.chunks_to_ass(chunks, font_name)
            with open(tmp_ass, "w", encoding="utf-8-sig") as f:
                f.write(content)
            overlay_times = ae.pick_overlay_times(tmp_ass)

        # 세션에 저장 (tmpdir은 2단계까지 유지)
        st.session_state.merged_tmpdir  = tmpdir
        st.session_state.merged_path    = merged
        st.session_state.subtitle_chunks = chunks
        st.session_state.clip_boundaries = clip_boundaries
        st.session_state.overlay_times   = overlay_times
        st.session_state.font_name       = font_name
        st.session_state.stage           = "edit_sub"
        return True

    except Exception as e:
        add_log(f"오류: {e}", "❌")
        st.exception(e)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False


def run_stage2(edited_chunks):
    """2단계: 수정된 자막으로 최종 영상 렌더링"""
    tmpdir     = st.session_state.merged_tmpdir
    merged     = st.session_state.merged_path
    font_name  = st.session_state.font_name
    clip_boundaries = st.session_state.clip_boundaries
    overlay_times   = st.session_state.overlay_times
    assets_dir = str(BASE_DIR / "assets")

    try:
        apply_settings()

        add_log("수정된 자막으로 ASS 생성 중...", "📝")
        ass_path = os.path.join(tmpdir, "result_edit.ass")
        content = ae.chunks_to_ass(edited_chunks, font_name)
        with open(ass_path, "w", encoding="utf-8-sig") as f:
            f.write(content)
        set_progress(20)

        # BGM
        bgm_tmp = None
        if bgm_file:
            bgm_tmp = os.path.join(tmpdir, "bgm_upload.mp3")
            with open(bgm_tmp, "wb") as f:
                f.write(bgm_file.getvalue())
            add_log(f"BGM: {bgm_file.name}  (볼륨 {bgm_vol}%)")

        out_path = os.path.join(tmpdir, "result_final.mp4")
        add_log("최종 렌더링 중 (자막·효과음·BGM 합성)...", "🎬")
        ae.render_final(merged, ass_path, tmpdir, out_path,
                        assets_dir, clip_boundaries, overlay_times,
                        bgm_path=bgm_tmp,
                        bgm_volume=bgm_vol / 100)
        set_progress(100)
        add_log("✅ 완성!", "🎉")

        with open(out_path, "rb") as f:
            return f.read()

    except Exception as e:
        add_log(f"오류: {e}", "❌")
        st.exception(e)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        st.session_state.merged_tmpdir = None


# ── 1단계 실행 트리거 ─────────────────────────────────────────
if run_btn and uploaded:
    st.session_state.processing = True
    st.session_state.done       = False
    st.session_state.stage      = "upload"
    st.session_state.log_lines  = []
    st.session_state.result_bytes = None

    ok = run_stage1(uploaded)
    st.session_state.processing = False
    if ok:
        st.rerun()

# ── 자막 수정 UI (1단계 완료 후) ─────────────────────────────
if st.session_state.stage == "edit_sub" and st.session_state.subtitle_chunks:
    st.markdown("---")
    st.markdown("### ✏️ 자막 수정")
    st.caption("오타나 잘못 인식된 부분을 수정하세요. 한 줄 = 자막 1개 (타이밍은 자동 유지)")

    chunks = st.session_state.subtitle_chunks
    default_text = "\n".join(text for _, _, text in chunks)

    edited_text = st.text_area(
        "자막 텍스트 (수정 후 아래 버튼 클릭)",
        value=default_text,
        height=min(400, max(150, len(chunks) * 22)),
        label_visibility="collapsed",
    )

    col_info, col_btn = st.columns([3, 1])
    with col_info:
        st.caption(f"총 {len(chunks)}줄 · 줄 수를 맞춰주세요 (줄 추가/삭제 가능)")
    with col_btn:
        finish_btn = st.button("🎬 영상 완성하기",
                               use_container_width=True, type="primary")

    if finish_btn:
        edited_lines = [l.strip() for l in edited_text.split("\n")]
        # 편집된 텍스트를 원래 타임스탬프와 매핑
        edited_chunks = []
        for i, (ts, te, _) in enumerate(chunks):
            txt = edited_lines[i] if i < len(edited_lines) else ""
            if txt:
                edited_chunks.append((ts, te, txt))

        st.session_state.processing = True
        st.session_state.log_lines  = []
        result = run_stage2(edited_chunks)
        st.session_state.processing = False

        if result:
            st.session_state.result_bytes = result
            st.session_state.result_name  = "result_편집완료.mp4"
            st.session_state.done         = True
            st.session_state.stage        = "done"
            st.rerun()

# ── 완료 배너 + 결과 미리보기 ────────────────────────────────
if st.session_state.done and st.session_state.result_bytes:
    st.markdown("---")
    st.success("✅ 편집 완료! 결과 영상을 확인하고 다운로드하세요.")
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
    if st.button("🔄 새 영상 편집하기"):
        for k in ["done","stage","subtitle_chunks","result_bytes","log_lines"]:
            st.session_state[k] = None if k != "stage" else "upload"
            if k == "log_lines":
                st.session_state[k] = []
            if k == "done":
                st.session_state[k] = False
        st.rerun()

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
