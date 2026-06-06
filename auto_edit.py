# -*- coding: utf-8 -*-
"""
AI 영상 편집 자동화
- input 폴더의 영상들을 파일 이름 순서로 읽음
- 말 없는 조용한 구간을 자동으로 잘라냄 (FFmpeg silencedetect)
- 영상들을 순서대로 합침
- 한국어 자막(ASS) 생성 + 영상 입힘
  · 화면 중앙 20pt 아래, 흰색 굵은 글자 + 검은 외곽선
  · 12자 단위로 분리 / 형광 연두색(#CCFF00) 강조 / 줌팝 등장 효과
- 효과음 자동 생성 후 삽입
  · 영상 시작 → sound.wav (assets에 있으면 사용, 없으면 자동 생성)
  · 클립 전환 → sound1.wav
- 이미지 오버레이 (assets/image.png, image1.png)
  · 자막 타이밍 기준 적절한 시점에 한 번씩 등장
  · 등장 시 줌인, 퇴장 시 줌아웃 효과

사용법:  python auto_edit.py
"""

import os
import re
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ============================================================
# 설정 (필요하면 이 값들만 바꾸세요)
# ============================================================
INPUT_DIR   = "input"
OUTPUT_DIR  = "output"
ASSETS_DIR  = "assets"

# --- 무음 제거 ---
SILENCE_DB       = -30
SILENCE_MIN_SEC  = 0.7
KEEP_PADDING_SEC = 0.20

# --- 자막 ---
WHISPER_MODEL      = "small"    # tiny / base / small / medium
LANGUAGE           = "ko"
SUBTITLE_FONT      = "GmarketSans"
SUBTITLE_FALLBACK  = "Malgun Gothic"
SUBTITLE_SIZE      = 72          # 글자 크기 (너무 크면 55~60 으로 줄이세요)
SUBTITLE_MAX_CHARS = 12
SUBTITLE_OFFSET_PT = 20         # 화면 중앙에서 아래로 (px)
HIGHLIGHT_COLOR    = "#CCFF00"  # 형광 연두색
TEXT_POP_MS        = 120        # 텍스트 줌팝 애니메이션 시간 (밀리초)
TYPEWRITER_MS      = 80         # 타자기 효과: 글자 하나 나타나는 시간 (밀리초)

# --- 이미지 오버레이 ---
OVERLAY_SIZE = 240      # 이미지 최대 크기 (px)
OVERLAY_DUR  = 2.0      # 이미지 표시 시간 (초)
ZOOM_RAMP    = 0.3      # 줌인/줌아웃 구간 (초)

# --- 출력 ---
TARGET_W = 1920
TARGET_H = 1080
FPS      = 30

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".wmv", ".flv")
FONT_EXTS  = (".ttf", ".otf")


# ============================================================
# 유틸
# ============================================================

def run(cmd, capture=False, cwd=None):
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=cwd)
    if cmd and cmd[0] == "ffmpeg":
        cmd = [cmd[0], "-hide_banner", "-loglevel", "error"] + cmd[1:]
    return subprocess.run(cmd, check=True, cwd=cwd)


def ffprobe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def hex_to_ass(hex_color, alpha=0):
    """#RRGGBB → &HaaBBGGRR& (ASS BGR 순서)"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}&"


def fmt_ts_ass(t):
    """초 → ASS 타임스탬프 H:MM:SS.cc"""
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60);   t -= m * 60
    s = int(t)
    cs = int(round((t - s) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def parse_ass_time(ts_str):
    """H:MM:SS.cc → float 초"""
    h, m, s_cs = ts_str.strip().split(":")
    s, cs = s_cs.split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100


# ============================================================
# 무음 제거 / 클립 처리
# ============================================================

def detect_silence(path):
    r = run(["ffmpeg", "-i", str(path), "-af",
             f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_SEC}",
             "-f", "null", "-"], capture=True)
    log = r.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends   = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else ffprobe_duration(path)
        silences.append((s, e))
    return silences


def speech_segments(path):
    dur = ffprobe_duration(path)
    silences = detect_silence(path)
    if not silences:
        return [(0.0, dur)]

    segs, cur = [], 0.0
    for s, e in silences:
        if s > cur:
            segs.append((cur, s))
        cur = max(cur, e)
    if cur < dur:
        segs.append((cur, dur))

    padded = [(max(0.0, s - KEEP_PADDING_SEC), min(dur, e + KEEP_PADDING_SEC))
              for s, e in segs]
    merged = []
    for s, e in padded:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s > 0.1]


def trim_and_normalize(src, dst, segments):
    if not segments:
        return False
    sel = "+".join([f"between(t,{s:.3f},{e:.3f})" for s, e in segments])
    vf = (f"select='{sel}',setpts=N/FRAME_RATE/TB,"
          f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
          f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}")
    af = f"aselect='{sel}',asetpts=N/SR/TB,aresample=async=1:first_pts=0"
    run(["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-af", af,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(dst)])
    return True


def concat_clips(clips, dst, tmpdir):
    listfile = os.path.join(tmpdir, "concat.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{Path(c).as_posix()}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", str(dst)])


# ============================================================
# 자막 생성 (ASS)
# ============================================================

_HIGHLIGHT_PAT = re.compile(
    r"[0-9,]+(?:[%배원만억천백십위초분](?:째)?)?"   # 숫자+단위
    r"|(?<!\w)[A-Z]{2,}(?!\w)"                     # 대문자 약어 (AI, GPT 등)
)

# 강조 제외할 짧은 조사/어미 (1~2자)
_KO_STOPS = {"은", "는", "이", "가", "을", "를", "에", "의", "로", "으로",
             "과", "와", "도", "만", "서", "에서", "부터", "까지", "한",
             "그", "이", "저", "것", "수", "때", "더", "또", "및"}


def apply_highlight(text):
    """숫자·약어 우선 강조, 없으면 가장 핵심적인 단어(가장 긴 단어) 강조"""
    lime  = hex_to_ass(HIGHLIGHT_COLOR)
    white = hex_to_ass("#FFFFFF")

    def rep(m):
        return f"{{\\c{lime}}}{m.group()}{{\\c{white}}}"

    result = _HIGHLIGHT_PAT.sub(rep, text)

    # 숫자·약어가 하나도 없으면 → 가장 긴 의미 단어 강조
    if result == text:
        words = text.split()
        # 조사/어미 제외, 2글자 이상인 단어 중 가장 긴 것
        candidates = [w for w in words if len(w) >= 2 and w not in _KO_STOPS]
        if candidates:
            key = max(candidates, key=len)
            # 첫 번째 등장만 강조
            result = text.replace(key, f"{{\\c{lime}}}{key}{{\\c{white}}}", 1)

    return result


def split_subtitle(text, t_start, t_end, max_chars=SUBTITLE_MAX_CHARS):
    words = text.split()
    chunks, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            while len(w) > max_chars:
                chunks.append(w[:max_chars])
                w = w[max_chars:]
            cur = w
    if cur:
        chunks.append(cur)
    if not chunks:
        return []
    total = sum(len(c) for c in chunks)
    entries, t = [], t_start
    for c in chunks:
        dur = (t_end - t_start) * (len(c) / total)
        entries.append((c, t, t + dur))
        t += dur
    return entries


def make_ass_header(font_name):
    white = hex_to_ass("#FFFFFF")
    black = hex_to_ass("#000000")
    return (
        f"﻿[Script Info]\n"
        f"ScriptType: v4.00+\n"
        f"PlayResX: {TARGET_W}\n"
        f"PlayResY: {TARGET_H}\n"
        f"ScaledBorderAndShadow: yes\n\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        f"OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        f"ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        f"Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{SUBTITLE_SIZE},{white},{black},{black},"
        f"&H00000000&,-1,0,0,0,100,100,0,0,1,3,1,5,10,10,0,1\n\n"
        f"[Events]\n"
        f"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def typewriter_lines(plain_text, styled_text, ts, te, cx, cy):
    """타자기 효과: 한 글자씩 순서대로 나타나는 Dialogue 라인 생성.

    - 전체 구간의 최대 60% 또는 글자수×TYPEWRITER_MS 중 짧은 쪽을 타이핑 구간으로 사용
    - 타이핑 중에는 평문(강조 없음), 완성 후에는 강조 버전으로 전환
    """
    chars = list(plain_text)
    n = len(chars)
    if n == 0:
        return []

    total_dur = te - ts
    # 타이핑에 쓸 최대 시간: 구간의 60% 또는 n×TYPEWRITER_MS 중 작은 값
    type_dur = min(total_dur * 0.60, n * TYPEWRITER_MS / 1000)
    char_dur = type_dur / n  # 글자당 실제 표시 시간

    pos = f"\\an5\\pos({cx},{cy})"
    lines = []

    for i in range(1, n + 1):
        partial = "".join(chars[:i])
        t_s = ts + (i - 1) * char_dur
        # 마지막 글자는 subtitle 끝까지, 나머지는 char_dur 간격
        t_e = ts + i * char_dur if i < n else te
        # 완성된 마지막 라인에만 강조 색상 적용
        display = styled_text if i == n else partial
        lines.append(
            f"Dialogue: 0,{fmt_ts_ass(t_s)},{fmt_ts_ass(t_e)},"
            f"Default,,0,0,0,,{{{pos}}}{display}"
        )

    return lines


def transcribe_to_ass(video, ass_path, font_name):
    from faster_whisper import WhisperModel
    print(f"  음성 인식 중 (모델: {WHISPER_MODEL}, CPU)... 영상 길이에 따라 시간이 걸립니다.")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(video), language=LANGUAGE,
                               vad_filter=True, beam_size=5)

    cx = TARGET_W // 2
    cy = TARGET_H // 2 + SUBTITLE_OFFSET_PT

    dialogues = []
    for seg in segs:
        text = seg.text.strip()
        if not text:
            continue
        for chunk_text, ts, te in split_subtitle(text, seg.start, seg.end):
            styled = apply_highlight(chunk_text)
            dialogues.extend(
                typewriter_lines(chunk_text, styled, ts, te, cx, cy)
            )

    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(make_ass_header(font_name))
        f.write("\n".join(dialogues))

    return len(dialogues)


def parse_ass_times(ass_path):
    """ASS 파일에서 (start, end) 타임스탬프 목록 반환"""
    times = []
    with open(ass_path, encoding="utf-8-sig") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) >= 3:
                    try:
                        ts = parse_ass_time(parts[1])
                        te = parse_ass_time(parts[2])
                        times.append((ts, te))
                    except Exception:
                        pass
    return times


def pick_overlay_times(ass_path):
    """image.png 과 image1.png 각 1회 삽입할 타이밍 선정"""
    times = parse_ass_times(ass_path)
    if not times:
        return []
    total = len(times)
    # 1/3 지점과 2/3 지점에 삽입 (영상 흐름에서 자연스러운 위치)
    idx0 = max(0, total // 3)
    idx1 = max(0, 2 * total // 3)
    result = []
    for i, idx in enumerate([idx0, idx1]):
        ts = times[idx][0]
        result.append((ts, OVERLAY_DUR, i))   # (시작, 지속, 0=image/1=image1)
    return result


# ============================================================
# 효과음 생성
# ============================================================

def generate_sfx(assets_dir):
    """효과음이 없으면 FFmpeg로 자동 생성"""
    s0 = os.path.join(assets_dir, "sound.wav")
    s1 = os.path.join(assets_dir, "sound1.wav")

    if not os.path.exists(s0):
        print("  sound.wav 자동 생성 중...")
        # 시작 효과음: 상승 후 감쇠하는 부드러운 톤
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", ("aevalsrc=0.35*sin(2*PI*(250+t*300)*t)*exp(-t*1.5)"
                    ":s=48000:c=stereo:d=0.9"),
             "-af", "afade=t=in:st=0:d=0.05,afade=t=out:st=0.7:d=0.2",
             "-ar", "48000", "-ac", "2", s0])

    if not os.path.exists(s1):
        print("  sound1.wav 자동 생성 중...")
        # 전환 효과음: 짧고 경쾌한 스윙업 클릭
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", ("aevalsrc=0.45*sin(2*PI*(400+t*800)*t)*exp(-t*12)"
                    ":s=48000:c=stereo:d=0.35"),
             "-af", "afade=t=in:st=0:d=0.02,afade=t=out:st=0.25:d=0.1",
             "-ar", "48000", "-ac", "2", s1])

    return s0, s1


# ============================================================
# 이미지 줌 클립 생성
# ============================================================

def make_zoom_clip(image_path, output_path):
    """이미지로 줌인 → 대기 → 줌아웃 영상 클립 생성"""
    dur = OVERLAY_DUR
    ramp = ZOOM_RAMP
    hold = dur - 2 * ramp

    # t 기반 zoom factor (0.02 → 1 → 1 → 0.02)
    z = (f"if(lt(t,{ramp}),max(0.02,t/{ramp}),"
         f"if(lt(t,{ramp+hold}),1,"
         f"max(0.02,({dur}-t)/{ramp})))")

    # 짝수 픽셀 보장
    half = OVERLAY_SIZE // 2
    w_expr = f"max(2,2*floor({half}*({z})))"
    h_expr = f"max(2,2*floor({half}*({z})))"

    vf = (
        f"scale={OVERLAY_SIZE}:{OVERLAY_SIZE}:force_original_aspect_ratio=decrease,setsar=1,"
        f"pad={OVERLAY_SIZE}:{OVERLAY_SIZE}:(ow-iw)/2:(oh-ih)/2,"
        f"scale=w='{w_expr}':h='{h_expr}':eval=frame,"
        f"pad={OVERLAY_SIZE}:{OVERLAY_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

    run(["ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
         "-t", str(dur), "-vf", vf, "-r", str(FPS),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output_path)])


# ============================================================
# 최종 렌더링 (자막 + 이미지 오버레이 + 효과음)
# ============================================================

def render_final(merged, ass_path, tmpdir, out_path,
                 assets_dir, clip_boundaries, overlay_times):
    """
    merged         : 합쳐진 무음제거 영상 (절대 경로)
    ass_path       : ASS 자막 파일 (절대 경로)
    tmpdir         : 임시 작업 폴더 (ASCII 경로)
    out_path       : 출력 결과물 (절대 경로)
    assets_dir     : assets 폴더 (절대 경로)
    clip_boundaries: [t1, t2, ...] 클립 전환 시점 (초)
    overlay_times  : [(ts, dur, 0or1), ...] 이미지 삽입 타이밍
    """

    # ── 파일을 임시 폴더로 복사 (한글 경로 우회) ──────────────
    shutil.copy(merged,   os.path.join(tmpdir, "main.mp4"))
    shutil.copy(ass_path, os.path.join(tmpdir, "subs.ass"))

    if os.path.isdir(assets_dir):
        for f in os.listdir(assets_dir):
            if f.lower().endswith(FONT_EXTS):
                shutil.copy(os.path.join(assets_dir, f), os.path.join(tmpdir, f))

    s0, s1 = generate_sfx(assets_dir)
    shutil.copy(s0, os.path.join(tmpdir, "sound.wav"))
    shutil.copy(s1, os.path.join(tmpdir, "sound1.wav"))

    # ── 이미지 줌 클립 생성 ───────────────────────────────────
    img_files = [os.path.join(assets_dir, "image.png"),
                 os.path.join(assets_dir, "image1.png")]
    zoom_clips = []   # (ts, dur, local_filename)
    for ts, dur, which in overlay_times:
        src = img_files[which]
        if os.path.exists(src):
            name = f"zoom_{len(zoom_clips)}.mp4"
            make_zoom_clip(src, os.path.join(tmpdir, name))
            zoom_clips.append((ts, dur, name))
            print(f"  이미지{which} 줌 클립 생성: t={ts:.1f}s")

    # ── FFmpeg 입력 목록 ──────────────────────────────────────
    # [0] main.mp4
    # [1] sound.wav
    # [2 .. 2+N-1] sound1.wav × N  (N = len(clip_boundaries))
    # [2+N .. ] zoom_N.mp4 × M
    N = len(clip_boundaries)
    inputs = ["-i", "main.mp4", "-i", "sound.wav"]
    for _ in clip_boundaries:
        inputs += ["-i", "sound1.wav"]
    for _, _, zname in zoom_clips:
        inputs += ["-i", zname]

    zoom_base_idx = 2 + N   # 줌 클립들의 첫 번째 입력 인덱스

    # ── filter_complex 빌드 ───────────────────────────────────
    fc = []

    # 1. 비디오: ASS 자막 굽기
    fc.append("[0:v]ass=subs.ass:fontsdir=.[vsub]")
    cur_v = "vsub"

    # 2. 비디오: 이미지 오버레이 (오른쪽 상단)
    for j, (ts, dur, _) in enumerate(zoom_clips):
        in_idx = zoom_base_idx + j
        te = ts + dur
        # 오른쪽 상단, 여백 20px
        x = f"W-{OVERLAY_SIZE}-20"
        y = "20"
        fc.append(
            f"[{cur_v}][{in_idx}:v]"
            f"overlay=x={x}:y={y}:shortest=0:enable='between(t,{ts},{te})'[v{j}]"
        )
        cur_v = f"v{j}"

    fc.append(f"[{cur_v}]copy[vout]")

    # 3. 오디오: 시작 효과음 (t=0)
    fc.append("[1:a]aformat=channel_layouts=stereo:sample_rates=48000,adelay=0|0[sa0]")

    # 4. 오디오: 전환 효과음 (각 클립 경계)
    for k, t_bound in enumerate(clip_boundaries):
        ms = int(t_bound * 1000)
        fc.append(
            f"[{2+k}:a]aformat=channel_layouts=stereo:sample_rates=48000,"
            f"adelay={ms}|{ms}[sa{k+1}]"
        )

    # 5. 오디오: 믹스
    n_effects = 1 + N   # 시작음 + 전환음
    all_audio = "[0:a]" + "".join(f"[sa{i}]" for i in range(n_effects))
    fc.append(f"{all_audio}amix=inputs={1+n_effects}:normalize=0:duration=first[aout]")

    # ── FFmpeg 실행 ───────────────────────────────────────────
    filter_str = ";".join(fc)

    cmd = (["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
           + inputs
           + ["-filter_complex", filter_str,
              "-map", "[vout]", "-map", "[aout]",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "192k",
              "final_out.mp4"])

    run(cmd, cwd=tmpdir)
    shutil.move(os.path.join(tmpdir, "final_out.mp4"), out_path)


# ============================================================
# 폰트 탐색
# ============================================================

def find_font(assets_dir):
    """폰트 탐색: assets → Linux 시스템 → Windows 폴백 순서"""
    # 1. assets 폴더의 GmarketSans
    if os.path.isdir(assets_dir):
        for f in os.listdir(assets_dir):
            if "gmarket" in f.lower() and f.lower().endswith(FONT_EXTS):
                return assets_dir, SUBTITLE_FONT

    # 2. Linux 시스템 한글 폰트 (Streamlit Cloud 등)
    linux_candidates = [
        ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",  "NanumGothicBold"),
        ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",       "NanumGothic"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",   "Noto Sans CJK KR"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  "DejaVu Sans"),
    ]
    for font_path, font_name in linux_candidates:
        if os.path.exists(font_path):
            return os.path.dirname(font_path), font_name

    # 3. Windows 폴백
    return None, SUBTITLE_FALLBACK


# ============================================================
# 메인
# ============================================================

def main():
    base       = Path(__file__).parent
    in_dir     = base / INPUT_DIR
    out_dir    = base / OUTPUT_DIR
    assets_dir = str(base / ASSETS_DIR)
    out_dir.mkdir(exist_ok=True)
    (base / ASSETS_DIR).mkdir(exist_ok=True)

    font_dir, font_name = find_font(assets_dir)
    if font_dir:
        print(f"[폰트] GmarketSans 발견: {assets_dir}")
    else:
        print(f"[폰트] assets 폴더에 GmarketSans 없음 → '{font_name}' 사용")

    # 이미지 확인
    has_img = [os.path.exists(os.path.join(assets_dir, f))
               for f in ("image.png", "image1.png")]
    if all(has_img):
        print("[이미지] image.png + image1.png 발견 → 오버레이 적용")
    else:
        print("[이미지] assets/image.png, image1.png 없음 → 오버레이 생략")

    videos = sorted([p for p in in_dir.iterdir()
                     if p.suffix.lower() in VIDEO_EXTS],
                    key=lambda x: x.name.lower())
    if not videos:
        print(f"\n[!] '{INPUT_DIR}' 폴더에 영상이 없습니다. 영상을 넣고 다시 실행하세요.")
        return

    print(f"\n[1/4] input 폴더에서 영상 {len(videos)}개 발견:")
    for v in videos:
        print(f"      - {v.name}")

    tmpdir = tempfile.mkdtemp(prefix="autoedit_")
    try:
        clips      = []
        clip_durs  = []

        print(f"\n[2/4] 무음 구간 제거 중...")
        for idx, v in enumerate(videos, 1):
            segs = speech_segments(v)
            dur  = ffprobe_duration(v)
            kept = sum(e - s for s, e in segs)
            print(f"      ({idx}/{len(videos)}) {v.name}: {dur:.1f}s → {kept:.1f}s "
                  f"(말하는 구간 {len(segs)}개)")
            clip = os.path.join(tmpdir, f"clip_{idx:03d}.mp4")
            if trim_and_normalize(v, clip, segs):
                clips.append(clip)
                clip_durs.append(kept)

        if not clips:
            print("[!] 처리할 구간이 없습니다.")
            return

        # 클립 전환 시점 (누적 시간, 마지막 제외)
        clip_boundaries = []
        acc = 0.0
        for d in clip_durs[:-1]:
            acc += d
            clip_boundaries.append(acc)

        print(f"\n[3/4] 영상 {len(clips)}개 합치는 중...")
        merged = os.path.join(tmpdir, "merged.mp4")
        if len(clips) == 1:
            shutil.copy(clips[0], merged)
        else:
            concat_clips(clips, merged, tmpdir)

        print(f"\n[4/4] 자막·효과음·이미지 처리 중...")
        ass_out = str(out_dir / "result.ass")
        n = transcribe_to_ass(merged, ass_out, font_name)
        print(f"      자막 {n}줄 생성")

        overlay_times = []
        if all(has_img) and n > 0:
            overlay_times = pick_overlay_times(ass_out)
            print(f"      이미지 오버레이: {len(overlay_times)}회")

        final_out = str(out_dir / "result.mp4")
        if n > 0:
            render_final(merged, ass_out, tmpdir, final_out,
                         assets_dir, clip_boundaries, overlay_times)
        else:
            print("      [안내] 인식된 음성이 없어 자막 없이 저장합니다.")
            shutil.copy(merged, final_out)

        print(f"\n[완료]")
        print(f"      영상: {out_dir / 'result.mp4'}")
        print(f"      자막: {out_dir / 'result.ass'}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
