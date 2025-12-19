# app.py
# Streamlit app: Copione PDF → Editor + Voce istantanea (Web Speech API) + Tap (Python WAV) + Salvataggio
# Update: performance migliorata
#  - Selettore multi-checkbox dei personaggi (in testa): solo i selezionati sono interattivi (edit + play/stop)
#  - Salvataggio anche dell'elenco personaggi selezionati
#  - Tap generato in Python come WAV (data URI) riprodotto nel click handler
#  - Personaggi non modificabili; testi plain con tasto Modifica; cache persistente

import io
import re
import json
import os
import base64
from typing import List, Dict, Optional, Tuple

import streamlit as st
import pdfplumber

from pydub import AudioSegment
from pydub.generators import Sine

CACHE_PATH = 'script_cache.json'

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
# TAP audio (Python → WAV base64 data URI, riutilizzabile)
# -------------------------------------------------------------

def generate_keyboard_tap(duration_ms: int = 1800,
                          avg_interval_ms: int = 140,
                          jitter_ms: int = 80,
                          volume_db: float = 0.0) -> AudioSegment:
    sr = 44100
    track = AudioSegment.silent(duration=duration_ms, frame_rate=sr)
    click_hi = Sine(1500).to_audio_segment(duration=22, volume=volume_db).fade_out(12)
    click_lo = Sine(180).to_audio_segment(duration=90, volume=volume_db-2).fade_out(60)
    click = click_hi.overlay(click_lo)
    import numpy as np
    rng = np.random.default_rng()
    t = 0
    while t < duration_ms:
        delta = int(max(60, rng.normal(avg_interval_ms, jitter_ms)))
        t += delta
        if t >= duration_ms:
            break
        track = track.overlay(click, position=t)
    track += AudioSegment.silent(duration=120, frame_rate=sr)
    return track


def tap_wav_data_uri(duration_ms: int = 1800) -> str:
    seg = generate_keyboard_tap(duration_ms=duration_ms)
    buf = io.BytesIO()
    seg.export(buf, format='wav')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return 'data:audio/wav;base64,' + b64

# -------------------------------------------------------------
# Cache: salva/legge sia blocks sia selected chars (retro-compatibile)
# -------------------------------------------------------------

def load_cache() -> Tuple[List[Dict[str, str]], List[str]]:
    if not os.path.exists(CACHE_PATH):
        return [], []
    try:
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):  # vecchio formato
            return data, []
        if isinstance(data, dict):
            return data.get('blocks', []), data.get('selected_chars', [])
    except Exception:
        pass
    return [], []

def save_cache(blocks: List[Dict[str, str]], selected_chars: List[str]):
    try:
        payload = { 'blocks': blocks, 'selected_chars': selected_chars }
        with open(CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# -------------------------------------------------------------
# UI
# -------------------------------------------------------------

st.set_page_config(page_title='Copione → Voce + Tap (WAV) + Salvataggio', layout='wide')
st.title('Copione PDF → Voce istantanea (Web Speech API) + Tap (Python WAV) + Salvataggio')

with st.sidebar:
    st.header('Impostazioni')
    uploaded_pdf = st.file_uploader('Carica PDF del copione', type=['pdf'])
    colA, colB = st.columns(2)
    with colA:
        if st.button('Salva ora'):
            save_cache(st.session_state.get('blocks', []), st.session_state.get('selected_chars', []))
            st.success('Copione salvato (cache locale).')
    with colB:
        if st.button('Reset cache'):
            try:
                os.remove(CACHE_PATH)
            except Exception:
                pass
            st.session_state['blocks'] = []
            st.session_state['selected_chars'] = []
            st.success('Cache rimossa.')

# Stato iniziale
if 'blocks' not in st.session_state or 'selected_chars' not in st.session_state:
    blocks_cached, selected_cached = load_cache()
    if 'blocks' not in st.session_state:
        st.session_state['blocks'] = blocks_cached
    if 'selected_chars' not in st.session_state:
        st.session_state['selected_chars'] = selected_cached

# Carica da PDF se presente
if uploaded_pdf is not None:
    st.session_state['blocks'] = parse_script_from_pdf(uploaded_pdf.read())
    # Default: nessun personaggio selezionato (pagina più leggera)
    st.session_state['selected_chars'] = []
    save_cache(st.session_state['blocks'], st.session_state['selected_chars'])

blocks = st.session_state['blocks']
selected_chars = set(st.session_state.get('selected_chars', []))

# TAP data URI globale
if 'tap_uri' not in st.session_state:
    st.session_state['tap_uri'] = tap_wav_data_uri(duration_ms=1800)

tap_uri = st.session_state['tap_uri']

if not blocks:
    st.info('Carica il PDF oppure ripristina la cache salvata. I personaggi non si modificano; i testi sono plain text con tasto Modifica; abilita interazione dal selettore personaggi qui sotto.')
else:
    # ====== SELETTORE PERSONAGGI (checkbox multiple) ======
    st.subheader('Seleziona i personaggi da rendere interattivi')
    # Estrai elenco personaggi (escludi SCENA)
    chars = [b['character'] for b in blocks if b['character'].strip().upper() != 'SCENA']
    uniq = sorted(dict.fromkeys(chars))

    # Mostra in 3 colonne per ridurre altezza
    cols_sel = st.columns(3)
    any_changed = False
    for idx, name in enumerate(uniq):
        col = cols_sel[idx % 3]
        with col:
            key = f'sel_{idx}'
            default = name in selected_chars
            val = st.checkbox(name, value=default, key=key)
            if val and name not in selected_chars:
                selected_chars.add(name); any_changed = True
            if (not val) and name in selected_chars:
                selected_chars.remove(name); any_changed = True
    # Aggiorna stato e salva se cambiato
    if any_changed:
        st.session_state['selected_chars'] = sorted(selected_chars)
        save_cache(blocks, st.session_state['selected_chars'])

    st.divider()

    # ====== RENDER BLOCS ======
    # Ottimizzazione: crea edit_flags solo per lunghezza blocks
    if 'edit_flags' not in st.session_state or len(st.session_state['edit_flags']) != len(blocks):
        st.session_state['edit_flags'] = [False]*len(blocks)

    # Render più leggero per i non selezionati
    for i, b in enumerate(blocks):
        char = b['character']
        is_scene = char.strip().upper() == 'SCENA'
        selected = (char in selected_chars) and (not is_scene)

        st.divider()
        cols = st.columns([1.2, 3, 1])
        with cols[0]:
            st.text_input('Personaggio', value=char, key=f'char_static_{i}', disabled=True)
        with cols[1]:
            if not selected:
                # Solo testo (plain) per non selezionati o SCENA
                st.markdown(f"<div style='white-space:pre-wrap;border:1px solid #ddd;padding:8px;border-radius:6px;background:#fafafa'>{b['text']}</div>", unsafe_allow_html=True)
            else:
                # Interattivo solo per selezionati
                if not st.session_state['edit_flags'][i]:
                    st.markdown(f"<div style='white-space:pre-wrap;border:1px solid #ddd;padding:8px;border-radius:6px;background:#fffef8'>{b['text']}</div>", unsafe_allow_html=True)
                else:
                    new_text = st.text_area('Modifica battuta', value=b['text'], height=140, key=f'text_edit_{i}')
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button('Salva', key=f'btnsave_{i}'):
                            blocks[i]['text'] = new_text
                            st.session_state['edit_flags'][i] = False
                            save_cache(blocks, sorted(selected_chars))
                            st.toast('Battuta salvata')
                    with c2:
                        if st.button('Annulla', key=f'btncancel_{i}'):
                            st.session_state['edit_flags'][i] = False
                if not st.session_state['edit_flags'][i]:
                    if st.button('Modifica', key=f'btnedit_{i}'):
                        st.session_state['edit_flags'][i] = True
        with cols[2]:
            if selected:
                use_tap = st.checkbox('Tap', value=True, key=f'tap_{i}', help='Riproduci suono tastiera prima della voce')
                # Bottone Play/Stop (solo per selezionati) con handler JS dedicato
                escaped = blocks[i]['text'].replace("'", "\\'").replace("\n", " ")
                html = f"""
                <div>
                  <button id='btn_{i}' style='padding:6px 10px;'>Play / Stop</button>
                  <audio id='tap_{i}' src='{tap_uri if use_tap else ''}' preload='auto'></audio>
                  <script>
                  (function(){{
                    const btn  = document.getElementById('btn_{i}');
                    const tap  = document.getElementById('tap_{i}');
                    const text = '{escaped}';
                    let playing = false;
                    let utter = null;
                    function selectVoice(){{
                      const voices = window.speechSynthesis.getVoices();
                      for (let v of voices){{
                        const name=(v.name||'').toLowerCase(); const vg=(v.lang||'').toLowerCase();
                        if (vg.startsWith('it') && (name.includes('female')||name.includes('fem')||name.includes('alice')||name.includes('donna'))) return v;
                      }}
                      for (let v of voices){{ if ((v.lang||'').toLowerCase().startsWith('it')) return v; }}
                      return null;
                    }}
                    function stopAll(){{
                      try{{ window.speechSynthesis.cancel(); }}catch(e){{}}
                      if (tap){{ try{{ tap.pause(); tap.currentTime = 0; }}catch(e){{}} }}
                      playing=false;
                    }}
                    btn.onclick = function(){{
                      if (playing){{ stopAll(); return; }}
                      playing = true;
                      utter = new SpeechSynthesisUtterance(text);
                      utter.lang = 'it-IT'; utter.rate = 1;
                      const v = selectVoice(); if (v) utter.voice = v;
                      utter.onend = stopAll; utter.onerror = stopAll;
                      if (tap && tap.src){{
                        tap.onended = function(){{ window.speechSynthesis.cancel(); window.speechSynthesis.speak(utter); }};
                        tap.onerror = function(){{ window.speechSynthesis.cancel(); window.speechSynthesis.speak(utter); }};
                        tap.play().catch(function(){{ window.speechSynthesis.cancel(); window.speechSynthesis.speak(utter); }});
                      }} else {{
                        window.speechSynthesis.cancel(); window.speechSynthesis.speak(utter);
                      }}
                    }};
                  }})();
                  </script>
                </div>
                """
                st.components.v1.html(html, height=60)

    # Esporta copione
    st.divider()
    colx, coly = st.columns(2)
    with colx:
        if st.button('Scarica copione TXT'):
            txt = io.StringIO()
            for b in blocks:
                txt.write(f"{b['character']}:\n{b['text']}\n\n")
            st.download_button('Scarica TXT', data=txt.getvalue(), file_name='copione_modificato.txt')
    with coly:
        if st.button('Scarica copione JSON'):
            data = json.dumps(blocks, ensure_ascii=False, indent=2)
            st.download_button('Scarica JSON', data=data, file_name='copione_modificato.json', mime='application/json')
