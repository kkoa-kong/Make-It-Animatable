"""UI grouping from existing English metadata; not source-provided categories."""

CLASSIFICATION = "keyword-v1"
CATEGORY_RULES = (
    ("dance", "舞蹈", (
        "dance", "dancing", "breakdance", "breakdancing", "hip hop", "salsa", "samba",
        "ballet", "tango", "rumba", "swing dance", "bellydance", "bellydancing", "uprock", "bboy",
    )),
    ("fitness", "健身", (
        "workout", "exercise", "fitness", "squat", "push up", "pushup", "sit up", "situp",
        "crunch", "curl", "stretch", "stretching", "burpee", "jumping jack", "box jump", "yoga",
    )),
    ("combat", "战斗", (
        "fight", "fighting", "punch", "punching", "kick", "kicking", "boxing", "attack",
        "attacking", "weapon", "rifle", "pistol", "gun", "sword", "shoot", "shooting",
        "combat", "capoeira", "uppercut", "jab", "assassination", "stabbing", "bayonet",
    )),
    ("locomotion", "移动", (
        "walk", "walking", "run", "running", "jog", "jogging", "sprint", "sprinting",
        "locomotion", "stairs", "stair", "turn", "turning", "sneak", "sneaking", "crawl", "crawling",
    )),
    ("acrobatics", "跳跃攀爬", (
        "jump", "jumping", "flip", "backflip", "back flip", "frontflip", "cartwheel",
        "climb", "climbing", "hang", "hanging", "parkour", "vault", "fall", "falling", "roll", "rolling",
    )),
    ("gestures", "姿态交互", (
        "wave", "waving", "gesture", "gesturing", "point", "pointing", "agreeing", "acknowledging",
        "greet", "greeting", "clap", "clapping", "cheer", "cheering", "salute", "saluting",
        "nod", "nodding", "shake", "shaking", "talk", "talking", "phone", "kiss", "laugh", "laughing",
        "cry", "crying", "shrug", "shrugging", "bow", "bowing", "handshake",
    )),
    ("idle", "待机休息", (
        "idle", "standing", "sit", "sitting", "seated", "breathing", "rest", "resting", "sleep", "sleeping",
    )),
    ("other", "其他动作", ()),
)
CATEGORY_LABELS = {category_id: label for category_id, label, _ in CATEGORY_RULES}


def classify(normalized_text: str) -> str:
    """Assign one primary group using ordered whole-word/phrase rules."""
    padded = f" {normalized_text} "
    for category_id, _, phrases in CATEGORY_RULES:
        if any(f" {phrase} " in padded for phrase in phrases):
            return category_id
    return "other"
