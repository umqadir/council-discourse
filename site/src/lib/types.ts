export type MeetingTag = "HEARING" | "VOTE" | "STATED_MEETING" | "LAND_USE";

export interface VideoSource {
  url: string;
  poster: string;
}

export interface Utterance {
  t_sec: number;
  speaker: string;
  text: string;
}

export interface Chapter {
  id: string;
  slug: string;
  type: string;
  title: string;
  summary: string;
  start_sec: number;
  end_sec: number;
  utterances: Utterance[];
}

export interface Meeting {
  slug: string;
  body: string;
  title: string;
  date: string;
  time: string;
  duration_sec: number;
  video: VideoSource;
  summary: string[];
  tags: MeetingTag[];
  chapters: Chapter[];
}

export interface BodySummary {
  slug: string;
  name: string;
  meetings: Meeting[];
}
