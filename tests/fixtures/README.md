# Fixtures

Real output, captured rather than written. Each file is exactly what
`whisper-cli -oj -np -nt` (whisper.cpp v1.9.4, `ggml-base.bin`) wrote, byte for byte
— tabs, key order and all — so a test that parses one is parsing the schema the
service will actually meet, not a tidied idea of it.

They are committed because the alternative is a test that downloads a 148 MB model
and spends a minute of CPU to assert on string handling, which is a test nobody runs.

| file | what it is | what it pins |
|---|---|---|
| `whisper-base-en.json` | one short utterance | the ordinary case, and that `result.language` carries the *detected* language |
| `whisper-base-multiseg.json` | a 48.9 s clip | **two** segments with distinct `offsets`, which is what proves segment bounds are read per segment and not from the first one |
| `whisper-base-silence.json` | 3 s of digital silence | that near-silence still produces a segment (`" you"`, a known whisper artefact) rather than an empty list — so "empty transcript" cannot be assumed to be how silence arrives |

## Regenerating

Not run by the tests, and not needed to run them. The audio was generated locally
(macOS `say` for speech, `ffmpeg -f lavfi -i anullsrc` for the silence), converted to
whisper's 16 kHz mono PCM, and transcribed in the same arm64 image the service uses:

```sh
say -o sample.aiff "Testing whisper output."
ffmpeg -y -i sample.aiff -ar 16000 -ac 1 -c:a pcm_s16le sample.wav
whisper-cli -m ggml-base.bin -f sample.wav -l auto -oj -of whisper-base-en -np -nt
```

The exact text is not what any test asserts on — the tests assert on *structure*, so
regenerating with different words does not break them. What would break them is the
schema changing, which is the point.
