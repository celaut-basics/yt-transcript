# yt-transcript

A YouTube video, downloaded and transcribed, packaged as a
[Celaut](https://github.com/celaut-project/nodo) service. Input is a video
URL; output is its transcription.

## Why

Downloading and parsing video from an arbitrary URL is exactly the kind of
thing worth confining to a microVM instead of running on your own machine or
node -- extractors and audio/video decoders are both frequent sources of
exploits, and a transcript is all the caller actually needs back. Sealing the
downloader and the transcriber behind a content-addressed spec gives the
caller text without either one ever touching anything else, the same
reasoning [file-as-service](https://github.com/celaut-basics/file-as-service)
gives for sealing a file with its interpreter.

## What it does

Given a YouTube URL:

1. Download the video's audio track.
2. Transcribe it to text.
3. Return the transcription as the service's output.

## Status

Idea stage -- this repo currently only states the intent. It will follow the
packaging convention used across [celaut-basics](https://github.com/celaut-basics)
(`.service/` for the Dockerfile, `service.json` and pack config; `service/`
for the implementation; `tests/`) once built.
