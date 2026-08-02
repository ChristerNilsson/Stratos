"""Generate Swedish square-coordinate sounds with a female voice.

Install the generator dependency once with::

    python -m pip install edge-tts

Then run::

    python make_sounds.py
"""

import asyncio
from pathlib import Path

try:
    import edge_tts
except ImportError as error:
    raise SystemExit(
        "edge-tts is required. Install it with: "
        "python -m pip install edge-tts"
    ) from error


OUTPUT_DIR = Path(__file__).resolve().parent / "sounds" / "squares" / "female"
VOICE = "sv-SE-SofieNeural"
MAX_ATTEMPTS = 3
PART_SOUNDS = {
    **{letter: letter for letter in "abcdefgh"},
    # "ätt" is a homophone of the number "ett" and avoids the voice
    # engine's incorrect pronunciation of the standalone number word.
    "1": "ätt",
    "2": "två",
    "3": "tre",
    "4": "fyra",
    "5": "fem",
    "6": "sex",
    "7": "sju",
    "8": "åtta",
}


def sounds_to_create() -> dict[str, str]:
    sounds = dict(PART_SOUNDS)
    for letter in "abcdefgh":
        for digit in "12345678":
            sounds[f"{letter}{digit}"] = (
                f"{PART_SOUNDS[letter]} {PART_SOUNDS[digit]}"
            )
    return sounds


async def create_sound(filename: str, spoken_text: str) -> None:
    output_path = OUTPUT_DIR / f"{filename}.mp3"
    temporary_path = output_path.with_suffix(".mp3.part")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        temporary_path.unlink(missing_ok=True)
        try:
            communication = edge_tts.Communicate(
                text=spoken_text,
                voice=VOICE,
            )
            await communication.save(temporary_path)
            if temporary_path.stat().st_size == 0:
                raise RuntimeError("TTS returned an empty file")
            temporary_path.replace(output_path)
            print(
                f"Created "
                f"{output_path.relative_to(Path(__file__).resolve().parent)}"
            )
            return
        except Exception:
            temporary_path.unlink(missing_ok=True)
            if attempt == MAX_ATTEMPTS:
                raise
            print(
                f"Attempt {attempt} failed for {filename}.mp3; retrying ..."
            )
            await asyncio.sleep(attempt)


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sounds = sounds_to_create()
    for filename, spoken_text in sounds.items():
        await create_sound(filename, spoken_text)
    print(f"Done: {len(sounds)} MP3 files created with {VOICE}.")


if __name__ == "__main__":
    asyncio.run(main())
