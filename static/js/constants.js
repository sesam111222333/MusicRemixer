// BS-RoFormer (vocals) + Demucs htdemucs_ft (drums/bass/other) is the
// only supported pipeline now -- it produces noticeably better vocal
// separation than the previous 6-stem htdemucs_6s and the user dropped
// guitar/piano because the 6-stem quality wasn't worth keeping.
export const STEM_NAMES = ["vocals", "drums", "bass", "other"];


export const STEM_DISPLAY = {
  vocals: "Vocals",
  drums: "Drums",
  bass: "Bass",
  other: "Other",
};

// FL Studio-style channel palette: saturated but slightly dusty, designed
// to read well on a dark background.
export const STEM_COLORS = {
  vocals: "#e85f6f",
  drums: "#e89048",
  bass: "#e8b848",
  other: "#88a8c8",
};

export const PROGRESS_COLOR = "#3a3a3a";

export const LOOP_DEFAULT_START_FRAC = 0.25;
export const LOOP_DEFAULT_END_FRAC = 0.5;

export const LANE_VOLUME_MAX = 2;