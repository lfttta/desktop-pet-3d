"""Action ids the assistant may ask the desktop pet to play.

The chat turn can request an animation, and the app plays it the same way the
tray menu does. Only one-shot clips are listed here, because that is the only
mode the app can drive from a reply without leaving the pet stuck:

* ``loop`` clips never fire ``animation_finished``, so a reply asking for one
  would leave the pet looping and the behaviour chain waiting forever.
* ``hold`` clips latch until the caller releases the request. The chat path has
  no release step, so ``pickup`` and ``fall`` would freeze the pet mid-pose;
  both are owned by the drag-and-drop state machine anyway.
* ``turn`` rewrites the body yaw over its clip, which the locomotion code owns.
* ``recover`` belongs to the landing path.
* ``male_sitting_pose`` is a single 0.033 s frame and reads as a flicker.

``walk`` is the one loop that is allowed, and it is not played as a clip: the
app turns it into a real walk segment, translating the window exactly as the
behaviour chain does. Playing the clip on its own would skate the feet in place.

The ids mirror ``animations/humanoid_core/manifest.json``; a test in
``tests/assets/test_chat_actions.py`` fails if the two drift apart.
"""

CHAT_ACTIONS = frozenset({
    'wave',
    'agreeing',
    'angry',
    'bashful',
    'bored',
    'carrying',
    'cheering',
    'clapping',
    'climbing',
    'climbing_down_wall',
    'climbing_ladder',
    'climbing_up_wall',
    'crying',
    'dancing',
    'dancing_twerk',
    'drinking',
    'excited',
    'gangnam_style',
    'happy',
    'hip_hop_dancing',
    'holding_down',
    'jump',
    'kiss',
    'laughing',
    'laying_nodding',
    'laying_severe_cough',
    'looking_around',
    'neck_stretching',
    'nervously_look_around',
    'pointing',
    'praying',
    'salsa_dancing',
    'salute',
    'scared',
    'shrugging',
    'singing',
    'sitting',
    'spinning',
    'standing_equip_bow',
    'standing_greeting',
    'surprised',
    'swinging',
    'taunt',
    'thinking',
    'victory',
    'yawn',
    'yelling',
})

## The empty string and ``none`` both mean "say it without acting it out".
NO_ACTION = frozenset({'', 'none'})

## Clips that are loop-mode in the library but still safe to request.
##
## ``idle`` is the resting state itself: playing it is the same as stopping.
## ``walk`` is not played as a clip — the app turns it into a walk segment so the
## window translates and the feet do not skate.
SPECIAL_ACTIONS = frozenset({'idle', 'walk'})


def allowed():
    """Every id a reply may name, including the two special ones."""
    return frozenset(NO_ACTION | SPECIAL_ACTIONS | CHAT_ACTIONS)


def prompt_list():
    """The action vocabulary as it appears in the output rules.

    Sorted so the prompt is stable between runs; the model only needs to see
    the ids, not the order the library happens to store them in.
    """
    return '/'.join(sorted(allowed() - NO_ACTION))
