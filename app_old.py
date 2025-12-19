# app.py
# Streamlit app: Copione PDF → Editor + Sintesi vocale locale macOS + Tap

import io
import os
import re
import subprocess
import tempfile
import base64
from typing import List, Dict, Optional

import streamlit as st
import streamlit.components.v1 as components
import pdfplumber
from pydub import AudioSegment
from pydub.generators import Sine
import numpy as np

# -------------------------------------------------------------
# Suono "tap"
# -------------------------------------------------------------

def generate_keyboard_tap(duration_ms: int = 1500,
                          avg_interval_ms: int = 120,
                          jitter_ms: int = 60,
                          volume_db: float = -12.0) -> AudioSegment:
    sr = 44100
    track = AudioSegment.silent(duration=duration_ms, frame_rate=sr)
    rng = np.random.default_rng()
    click_hi = Sine(1200).to_audio_segment(duration=10, volume=volume_db).fade_out(8)
    click_lo = Sine(220).to_audio_segment(duration=20, volume=volume_db-6).fade_out(15)
    click = click_hi.overlay(click_lo)
    t = 0
    while t < duration_ms:
        delta = int(max(40, rng.normal(avg_interval_ms, jitter_ms)))
        t += delta
        if t >= duration_ms:
            break
        track = track.overlay(click, position=t)
    return track + AudioSegment.silent(duration=150)

# -------------------------------------------------------------
# Sintesi vocale locale macOS (comando 'say')
# -------------------------------------------------------------

def synthesize_speech(text: str, voice: str = "Alice") -> Optional[AudioSegment]:
    try:
        with tempfile.NamedTemporaryFile(suffix='.aiff', delete=False) as tmp:
            out_path = tmp.name
        # Usa il comando 'say' di macOS per generare audio
        subprocess.run(['say', '-v', voice, '-o', out_path, text], 
                      check=True, capture_output=True)
        speech = AudioSegment.from_file(out_path)
        os.remove(out_path)
        return speech
    except Exception:
        return None

# -------------------------------------------------------------
# Parser PDF → blocchi {character, text}
# -------------------------------------------------------------

def _preclean_text(text: str) -> str:
    text = text.replace('\r', '')
    text = re.sub(r'\*{1,3}', '', text)
    text = re.sub(r'^_+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*_+$', '', text, flags=re.MULTILINE)
    text = text.replace('\\(', '(').replace('\\)', ')')
    text = re.sub(r'\s*:\s*', ': ', text)
    text = re.sub(r'[\t\x0b\x0c\u00A0]+', ' ', text)
    return text

def _is_section_heading(line: str) -> bool:
    u = line.upper()
    return (u.startswith('SCENA ') or 'PERSONAGGI' in u or 'ANTIPASTO' in u or 'PRIMI' in u or 'DOLCI' in u or 'CAFF' in u)

def _clean_stage_dirs_start(s: str) -> str:
    s = re.sub(r'^\((?:[^()]|\([^)]*\))*\)\s*', '', s)
    return s.strip()

def parse_script_from_pdf(file_bytes: bytes) -> List[Dict[str, str]]:
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        pages = [(p.extract_text() or '') for p in pdf.pages]
    text = _preclean_text("\n".join(pages))
    speaker_pat = re.compile(r"^(?P<who>[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ’' .\-]*(?:\([^)]*\))?)\s*:\s*(?P<what>.*)$")
    blocks: List[Dict[str, str]] = []
    current_name: Optional[str] = None
    current_lines: List[str] = []
    def flush():
        nonlocal current_name, current_lines
        if current_name and current_lines:
            joined = "\n".join(current_lines).strip()
            if joined:
                blocks.append({"character": current_name, "text": joined})
        current_name, current_lines = None, []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_name and current_lines and current_lines[-1] != '':
                current_lines.append('')
            continue
        if _is_section_heading(line):
            flush()
            blocks.append({"character": "SCENA", "text": line})
            continue
        m = speaker_pat.match(line)
        if m:
            flush()
            who = re.sub(r'\s*\([^)]*\)\s*', '', m.group('who')).strip()
            who = re.sub(r'\s{2,}', ' ', who)
            what = _clean_stage_dirs_start(m.group('what'))
            current_name, current_lines = who, ([what] if what else [])
        else:
            if line.startswith('(') and line.endswith(')'):
                flush()
                blocks.append({"character": "SCENA", "text": line})
            else:
                if current_name:
                    line2 = _clean_stage_dirs_start(line)
                    if line2:
                        current_lines.append(line2)
                else:
                    blocks.append({"character": "SCENA", "text": line})
    flush()
    compact: List[Dict[str, str]] = []
    for b in blocks:
        if compact and b['character'] == 'SCENA' and compact[-1]['character'] == 'SCENA':
            compact[-1]['text'] = (compact[-1]['text'] + '\n' + b['text']).strip()
        else:
            compact.append(b)
    return compact

# -------------------------------------------------------------
# UI
# -------------------------------------------------------------

st.set_page_config(page_title='Copione → Voce locale', layout='wide')
st.title('🎭 Copione PDF → Editor + Sintesi vocale locale + Tap')

with st.sidebar:
    st.header('Impostazioni')
    use_tap_default = st.checkbox('Suono di tap prima della battuta', value=True)
    voice_choice = st.selectbox('Voce macOS', ['Alice', 'Eddy', 'Flo', 'Reed', 'Sandy', 'Shelley'], index=0)
    uploaded_pdf = st.file_uploader('Carica PDF del copione', type=['pdf'])
    st.info('💡 TTS locale: comando `say` di macOS (istantaneo)')

if 'blocks' not in st.session_state:
    st.session_state['blocks'] = []
if 'last_pdf_name' not in st.session_state:
    st.session_state['last_pdf_name'] = None

# Carica il PDF solo se è nuovo (non già caricato)
if uploaded_pdf is not None:
    if st.session_state['last_pdf_name'] != uploaded_pdf.name:
        st.session_state['blocks'] = parse_script_from_pdf(uploaded_pdf.read())
        st.session_state['last_pdf_name'] = uploaded_pdf.name

blocks = st.session_state['blocks']

if not blocks:
    st.info("Carica il PDF del copione (formato: Nome: testo)")
else:
    st.success(f'Blocchi individuati: {len(blocks)}')
    for i, b in enumerate(blocks):
        st.divider()
        cols = st.columns([1.2, 3, 1])
        with cols[0]:
            new_char = st.text_input('Personaggio', value=b['character'], key=f'char_{i}')
        with cols[1]:
            new_text = st.text_area('Battuta', value=b['text'], height=140, key=f'text_{i}')
        blocks[i] = {"character": new_char, "text": new_text}
        with cols[2]:
            use_tap = st.checkbox('Tap', value=use_tap_default, key=f'tap_{i}')
            if st.button('▶️ Play', key=f'play_{i}'):
                speech = synthesize_speech(new_text, voice_choice)
                if speech is None:
                    st.error('Errore sintesi vocale')
                else:
                    audio = speech
                    if use_tap:
                        audio = generate_keyboard_tap(1500) + audio
                    # Converti in base64 e riproduci con JS immediato
                    buf = io.BytesIO()
                    audio.export(buf, format='mp3')
                    buf.seek(0)
                    b64 = base64.b64encode(buf.read()).decode()
                    audio_html = f"""
                    <audio id="audio_{i}" src="data:audio/mp3;base64,{b64}"></audio>
                    <script>
                        document.getElementById('audio_{i}').play();
                    </script>
                    """
                    components.html(audio_html, height=0)
