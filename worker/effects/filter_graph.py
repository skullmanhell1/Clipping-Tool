"""The ffmpeg filter graph as an object, rather than as two lists and a naming convention.

``compositor.render_clip`` builds one ``-filter_complex`` graph out of roughly twenty-five
optional phases. Before this module it did so by appending to two plain lists — ``inputs`` (argv
fragments) and ``graph_parts`` (graph segments) — and by writing the intermediate labels out as
string literals: ``vlook``, ``vbroll``, ``vbase``, ``vout``, ``vbrand`` on the video side and
``aclean``, ``aout``, ``aeng``, ``aloud``, ``apeak`` on the audio side.

Two things about that were only safe by convention.

**Layering was expressed as append order.** The segments are joined with ``";"`` in list order, so
a phase appended in the wrong place produces a graph that ffmpeg accepts and renders differently.
Nothing raises. The b-roll label made this concrete: ``vlook`` was *referenced* as the b-roll
overlay's base before the segment that *creates* it was appended, so the name existed in two
places and had to be spelled the same in both.

**Input indices were derived by counting ``"-i"`` occurrences in argv fragments.** Three
interacting offsets — the music input, the b-roll block and the emoji block — were each computed
as arithmetic over ``engine_input_args.count("-i")`` and ``broll_input_args.count("-i")``, at a
point in the function *before* the corresponding arguments had been appended. The accounting was
correct, and it was correct in a way that could not be checked locally: the offsets are computed
around line 500 and the arguments they describe are appended at 650 and 740, so the invariant
holding depends on those three appends staying in an order stated nowhere except a comment.

An over-count by one is the dangerous case. Pointed past the end of the input list, ffmpeg fails
loudly. Pointed at the *wrong existing input* — a b-roll offset landing inside the emoji block —
ffmpeg is entirely happy and composites the wrong image at the right time. That is the silent
visual regression this exists to make unrepresentable.

**What this class owns:** input registration and index handout, segment order, and serialisation.
**What it deliberately does not own:** the filter strings themselves. Those come from
``overlays``, ``broll``, ``emoji``, ``branding``, ``audio`` and ``captions``, and the graph is not
the right place to know what a zoompan expression looks like.

**Byte-identical output is the whole point.** ``inputs()`` returns exactly the argv this replaced
and ``filter_complex()`` exactly the ``";"``-joined string, so every frozen golden in
``tests/test_kinetic_compositor.py`` passes unchanged. The class adds no formatting of its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


class FilterGraph:
    """An ordered ffmpeg filter graph plus its input list.

    Not a validating graph. It does not check that a label is produced before it is consumed, and
    that is a deliberate limit rather than an omission: two of the segments handed to :meth:`add`
    arrive as pre-built multi-segment strings that declare and consume their own internal labels
    (``branding.logo_filter`` emits ``movie=…[brandlogo];[base][brandlogo]overlay=…[out]``, and
    ``broll.build_broll_overlay`` emits one segment per cue), so a reachability check here would
    have to parse other modules' output. Ordering and index handout are what were actually going
    wrong; those are what this owns.
    """

    def __init__(self) -> None:
        self._inputs: list[str] = []
        self._input_count = 0
        self._segments: list[str] = []

    # -- inputs ------------------------------------------------------------
    def add_input(self, path: str, *, prefix: Sequence[str] = ()) -> int:
        """Register one input and return **its** index.

        ``prefix`` carries the options that must precede ``-i`` for this input (``-loop 1``,
        ``-t 3.5``). They are part of the input, not separate arguments, which is why they are a
        parameter here rather than a separate call: the previous code appended them alongside the
        ``-i`` and then counted ``"-i"`` tokens to avoid counting them, and that only worked
        because nobody ever added an option whose *value* was the string ``-i``.
        """
        index = self._input_count
        self._inputs.extend(prefix)
        self._inputs.extend(["-i", path])
        self._input_count += 1
        return index

    def add_input_args(self, args: Iterable[str]) -> int:
        """Register a pre-built argv fragment, returning the index of its **first** input.

        For fragments produced elsewhere — the reserved engine block, and the b-roll and emoji
        overlay builders, each of which emits its own ``-loop``/``-t``/``-i`` groups. The count
        still comes from counting ``"-i"``, because that is the only structure a flat argv
        fragment has; what has changed is that the count and the append now happen in the same
        call, so they cannot disagree.

        Returns the index the first input *would* take, which equals :meth:`next_input_index`
        before the call. Safe to call with an empty fragment.
        """
        args = list(args)
        first = self._input_count
        self._inputs.extend(args)
        self._input_count += args.count("-i")
        return first

    def next_input_index(self) -> int:
        """The index the next registered input will take.

        This is what replaces the offset arithmetic. ``broll.build_broll_overlay`` and
        ``emoji.build_overlay`` both need to know their first index *before* they can emit the
        argv fragment that occupies it, so the index has to be readable ahead of registration —
        but reading it from the graph means it is derived from what has actually been registered
        rather than recomputed from a predicate about what will be.
        """
        return self._input_count

    @property
    def input_count(self) -> int:
        """How many inputs are registered."""
        return self._input_count

    def inputs(self) -> list[str]:
        """The argv input fragment, in registration order."""
        return list(self._inputs)

    # -- segments ----------------------------------------------------------
    def chain(self, input_label: str, filters: Sequence[str], output_label: str) -> str:
        """Append ``[input_label]f1,f2,…[output_label]`` and return ``output_label``.

        Returning the output label is what lets a caller thread the chain without restating the
        name: ``label = graph.chain(label, filters, "vbase")`` reads as "this phase consumed the
        current label and produced vbase", which is the relationship the old
        ``graph_parts.append(...)`` followed by a separate ``video_label = "vbase"`` split across
        two statements.

        Appends nothing and returns ``input_label`` unchanged when ``filters`` is empty, so a
        phase that turns out to contribute no filters does not emit a pass-through segment. That
        matters for byte-identity: the previous code guarded every append with ``if chain:`` and
        an empty segment would both change the string and leave a label defined but unused.
        """
        if not filters:
            return input_label
        self._segments.append(f"[{input_label}]{','.join(filters)}[{output_label}]")
        return output_label

    def add(self, segment: str) -> None:
        """Append a pre-built segment verbatim.

        The segment may itself contain ``";"`` and declare intermediate labels — see the class
        docstring. Empty and ``None`` segments are ignored so callers can pass a builder's result
        straight through: ``branding.logo_filter`` returns ``None`` when no logo is configured and
        ``broll.build_broll_overlay`` returns ``""`` when it degrades.
        """
        if segment:
            self._segments.append(segment)

    def filter_complex(self) -> str:
        """The ``-filter_complex`` value: every segment, in order, joined with ``";"``."""
        return ";".join(self._segments)

    def __bool__(self) -> bool:
        """Whether any segment was added — i.e. whether ``-filter_complex`` is needed at all."""
        return bool(self._segments)

    def __len__(self) -> int:
        return len(self._segments)
