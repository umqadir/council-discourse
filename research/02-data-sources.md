# NYC Council Meeting Ingestion — Data Source Map

Research conducted 2026-07-01. All API calls were executed live; output snippets are actual observed responses.

## 1. Legistar Web API (`webapi.legistar.com/v1/nyc`)

**Status: works, but NYC requires a free API token.** Anonymous requests fail:

```
$ curl -sv "https://webapi.legistar.com/v1/nyc/bodies?\$top=1"
< HTTP/1.1 403 Token is required
```

Tokens are issued via a simple name+email form at [council.nyc.gov/legislation/api](https://council.nyc.gov/legislation/api/) ("You will receive an email with the API Key and further instruction"). The token is passed as a `token=` URL parameter. This is the one manual prerequisite for the whole pipeline.

The API is standard Granicus Legistar OData (docs: webapi.legistar.com/Help, examples: webapi.legistar.com/Home/Examples). Schema verified live against an open client (Seattle — identical schema across clients):

```
$ curl "https://webapi.legistar.com/v1/seattle/events?$top=1&$orderby=EventDate desc"
{
  "EventId": 6787,
  "EventGuid": "F539683F-...",
  "EventLastModifiedUtc": "2026-06-17T20:04:35.503",
  "EventBodyId": 211,
  "EventBodyName": "Council Briefing",
  "EventDate": "2026-07-20T00:00:00",
  "EventTime": "2:00 PM",
  "EventLocation": "Council Chamber, City Hall...",
  "EventAgendaStatusName": "Cancelled",
  "EventAgendaFile": "https://legistar2.granicus.com/seattle/meetings/2026/7/6787_A_....pdf",
  "EventVideoStatus": "Public",
  "EventVideoPath": null,          <- exists in schema; NYC populates via InSite instead (see below)
  "EventInSiteURL": "https://seattle.legistar.com/MeetingDetail.aspx?LEGID=6787&...",
  "EventItems": []
}
```

Key endpoints (all `https://webapi.legistar.com/v1/nyc/...&token=TOKEN`):

| Endpoint | Purpose | Key fields |
|---|---|---|
| `/events?$filter=EventDate ge datetime'2026-07-01'` | upcoming meetings | EventBodyName, EventDate/EventTime, EventAgendaFile (PDF), EventInSiteURL |
| `/events?$filter=EventLastModifiedUtc gt datetime'...'` | incremental sync | EventLastModifiedUtc is the change cursor |
| `/events/{id}/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1` | agenda line items | EventItemAgendaSequence, EventItemTitle, EventItemMatterId/File/Name/Type, EventItemMatterAttachments, EventItemActionName, EventItemVideo/VideoIndex (video chapter offsets where populated) |
| `/bodies` | committees | BodyId, BodyName, BodyTypeName, BodyActiveFlag, BodyNumberOfMembers |
| `/officerecords?$filter=OfficeRecordBodyId eq {id}` | committee membership | OfficeRecordFullName, OfficeRecordTitle ("Chair"...), Start/EndDate, OfficeRecordPersonId |
| `/persons` | member records | contact info, WWW link |
| `/matters/{id}`, `/matters/{id}/attachments` | legislation detail | MatterFile ("Int 1234-2026"), MatterTitle, MatterStatusName, attachment PDFs |

OData paging: `$top` (max 1000), `$skip`, `$filter`, `$orderby`. No published hard rate limit; the civic-tech ecosystem (e.g. `opencivicdata/scrapers-us-municipal`) runs on this API.

**InSite (`legistar.council.nyc.gov`) — no token needed, and it's the video join key.** `Calendar.aspx` returns 200 to plain curl (905 KB HTML). Each meeting row has:
- `MeetingDetail.aspx?ID=1418199&GUID=D910E40A-...` (detail page; agenda PDF at `View.ashx?M=A&ID={id}&GUID={guid}`)
- a Video link of the form `Video.aspx?Mode=Auto&URL=<base64>` where the base64 decodes to the viebit VOD URL, e.g. → `https://councilnyc.viebit.com/vod/?s=true&v=NYCC-PV-CH-CHA_260615-100847.mp4`

This is how you map a Legistar event to its video file. (In the API, NYC's `EventVideoPath`/`EventMedia` are unreliable; the InSite calendar link is ground truth — recheck once token in hand.)

## 2. Video sources

### Viebit (primary archive) — `councilnyc.viebit.com`, CDN `vbfast-vod.viebit.com`

Granicus-era video was replaced by VieBit (LEIGHTRONIX) hosting. This is the Council's authoritative VOD archive, covering every hearing room. **Direct MP4 download works with plain curl** — verified:

```
$ curl -I https://vbfast-vod.viebit.com/counciln/qFAxOQb56lhjkl8g/NYCC-PV-CH-CHA_260615-100847.mp4
HTTP/2 200
content-type: video/mp4
accept-ranges: bytes        (range requests verified; that file is 2.2 GB)
```

URL structure:
- VOD page: `https://councilnyc.viebit.com/vod/?s=true&v={FILENAME}.mp4` → HTML contains `player.php?hash={HASH}` (16-char hash)
- Direct MP4: `https://vbfast-vod.viebit.com/counciln/{HASH}/{FILENAME}.mp4`
- **Closed captions (VTT)**: same path, `.vtt` extension — verified present and substantive (broadcast roll-up CC: ALL CAPS, duplicated overlapping cues — needs cleanup, but timestamped text for free)
- Thumbnail: same path, `.jpg`
- **RSS feed of new VODs**: `https://councilnyc.viebit.com/rss.xml` — 60 most recent items, each with filename (title), player hash (guid), pubDate. Observed latency: **same day, ~1-2 h after the meeting ends**. Cleanest discovery mechanism.

Filename convention encodes room + start timestamp `YYMMDD-HHMMSS`:
- `NYCC-PV-CH-CHA_*` — City Hall Council Chambers
- `NYCC-PV-CH-COM_*` — City Hall Committee Room
- `NYCC-250-8-1/-2/-3_*` — 250 Broadway hearing rooms
- occasional `...fix.mp4` re-uploads

yt-dlp does **not** support viebit — irrelevant, plain HTTP GET works.

### YouTube — @NYCCouncil, channel ID `UCu8AwLHSpgiFtKvHi_TFHog`

yt-dlp works. `/videos` tab is mostly short-form promo; **full meetings are on `/streams`** as archived live streams ("LIVE: ..." titles, inconsistent, interleaved with rallies/pressers — needs fuzzy date/title matching to map to Legistar events). Coverage incomplete relative to viebit. Verdict: **viebit primary; YouTube fallback.** Live streams also at council.nyc.gov/livestream (embeds viebit live players).

## 3. Existing captions/transcripts

Three tiers, fastest to slowest:

1. **Viebit VTT closed captions** — same-day, predictable URL. Real-time stenographer/CC quality: usable for chaptering and search, rough for quotes.
2. **YouTube auto-captions** — verified: automatic captions only, no manual English subtitles. Available a few hours after stream ends.
3. **Official transcripts on Legistar** — confirmed. Vendor-produced (Ubiqus, World Wide Dictation) PDF transcripts attached to events at `legistar.council.nyc.gov/View.ashx?...&M=F`. Appear **weeks to months after** the hearing. Via API they surface as event attachments; poll `EventLastModifiedUtc` to catch late additions. Good for retroactive quality upgrades and WER ground truth; useless for same-day publishing.

## 4. Council member & committee roster

- **Legistar API** (once token in hand): `/bodies` (`BodyActiveFlag eq 1`), `/officerecords` (membership w/ Chair/Member titles + dates), `/persons`. Same-system, always current, links members to matters/votes by PersonId.
- **NYC Open Data (Socrata, no auth)**:
  - `uvw5-9znb` — "City Council Members (1999 to Present)" (updated 2026-04-10)
  - `aabe-yfm9` — "City Council Committee Membership (1999 to 2025)" (updated 2025-12-30)
  - `curl 'https://data.cityofnewyork.us/resource/uvw5-9znb.csv?$limit=9999999'`
  - Update lag — fine for historical joins; use Legistar for "current".
- `council.nyc.gov/districts/` for photos/bios/district pages.

## 5. Other bodies (secondary)

citymeetings.nyc covered: City Council (primary), NYC Charter Revision Commission, one City Planning Commission hearing. For expansion:
- **CPC**: NYC Dept of City Planning YouTube channel `UCu0amGxQJBNtd1YlTFETTqQ` (yearly playlists); agendas on nyc.gov/planning. No Legistar.
- **MTA Board**: @mta-live YouTube + mta.info/transparency/board-and-committee-meetings (agendas/board books, 4th week monthly except Aug).
- **Community boards**: fragmented — 59 boards, individual YouTube channels, no unified source.

## 6. Rate limits / terms

| Source | Auth | Limits/terms |
|---|---|---|
| Legistar API | free token (email form) | No published rate limit; `$top` capped at 1000/page. Read-only public access is the stated purpose. |
| legistar InSite | none | Plain HTML, no bot blocking observed. |
| Viebit VOD/RSS/VTT | none | CDN (Varnish/Fastly + nginx), `cache-control: max-age=604800`; direct file access by design. Don't hammer; one meeting is 2+ GB. |
| YouTube | none for yt-dlp | yt-dlp against ToS strictly; Data API v3 (free, 10k units/day) is the sanctioned metadata route. Fallback only. |
| NYC Open Data | optional app token | Throttled without token; we have `NYC_OPENDATA_APP_TOKEN`. |

## Recommended ingestion path (daily cron)

1. **One-time**: request Legistar API token at council.nyc.gov/legislation/api.
2. **Discover meetings** (every few hours): `GET /v1/nyc/events?$filter=EventLastModifiedUtc gt datetime'{last_sync}'&token=...` — new/rescheduled/cancelled meetings + late attachments. Store EventId, body, date/time, EventAgendaFile, EventInSiteURL.
3. **Discover videos** (hourly on meeting days): poll `https://councilnyc.viebit.com/rss.xml`. New item → parse HASH (guid) + FILENAME (title) → download MP4 + sibling `.vtt`. Videos land ~1-2 h after adjournment.
4. **Join video ↔ event**: fetch Legistar `Calendar.aspx` (or event's MeetingDetail.aspx), decode the base64 in its `Video.aspx?Mode=Auto&URL=` link → viebit filename. Backstop: match room prefix + `YYMMDD-HHMMSS` vs EventDate/EventTime.
5. **Agenda structure**: `GET /events/{id}/eventitems?Attachments=1` for itemized agenda (matters, sponsors, attachments) to drive chapter extraction and linking.
6. **Transcribe**: run ASR on the MP4 (viebit VTT gives free timestamps/keywords for alignment and speaker-change hints); publish same-day.
7. **Backfill** (weekly): re-poll events from past ~90 days for official transcript attachments; cross-reference certified text.
8. **Roster** (weekly): refresh `/bodies` + `/officerecords` + `/persons`; seed historical from Socrata.

The single fragile link is step 4's base64 scrape of InSite; everything else is stable structured data. Once token arrives, verify whether NYC populates `EventVideoPath`/`EventMedia` in the API — if so, step 4 collapses into step 2.
