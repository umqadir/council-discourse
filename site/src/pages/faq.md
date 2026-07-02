---
layout: ../layouts/MarkdownLayout.astro
title: FAQ
description: How Council Discourse sources its data, makes transcripts, and handles corrections.
---

## Where does the data come from?

Meeting schedules, agendas, and related documents come from the New York City Council's Legistar system. The video comes from the Council's public video archive. Council Discourse re-hosts a copy of each meeting's video so that chapter and transcript timestamps can seek to exact moments.

## How are the transcripts made?

Each meeting's audio is transcribed by automated speech recognition, which produces the words and the per-line timestamps. Language models then identify speakers, divide the meeting into chapters, and write the summaries. The timestamps you click in a transcript come from the speech recognition layer, not from the language models.

## What does Council Discourse cover?

Meetings of the New York City Council, including committee hearings and stated meetings. It does not cover community boards, state or federal bodies, or other cities.

## How soon are meetings published?

Meetings are generally processed and published the same day or the next day after the recording becomes available.

<h2 id="transcription-errors">About transcription errors</h2>

The transcripts, chapters, summaries, and speaker labels are generated automatically and are not official. Automated speech recognition can mishear names, legislation numbers, and fast exchanges, and the speaker identification can attribute a line to the wrong person. Use the meeting video as the source of truth when precision matters.

<h2 id="report-an-issue">Reporting an issue or a correction</h2>

If you find a mistake, email [hello@example.org](mailto:hello@example.org) (TODO-DOMAIN: replace with the real address before launch) with the meeting, the chapter, the timestamp, and the correction. Reports about misheard names, misattributed speakers, or wrong wording are especially helpful.

## Is Council Discourse affiliated with the Council or citymeetings.nyc?

No. It is an independent project, not affiliated with the New York City Council or with citymeetings.nyc, the project that inspired it.
