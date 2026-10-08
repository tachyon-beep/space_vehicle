# Evidence and unresolved model inputs

Keep the existing provenance classes: `apollo`, `historical`, `derived`,
`chosen`, `UNCONFIGURED` (see [AGENTS.md](../AGENTS.md)). They describe distinct
claims. A declared value is not evidence that an applicable physical relation
has been implemented or independently validated.

For every input or rule that matters, record: the exact question; primary source
and page/section (or frozen-corpus file and line); hardware/mission effectivity;
configuration and operating regime; units and uncertainty/bounds; the derivation
or measurement; its consumer in this model; and the independent validation case.
A source about a different variant is context until applicability is established.

The modeled vehicle is an Apollo-shaped composite. Do not silently merge variant
geometry, propulsion constants or operating envelopes into an apparently flown
configuration. Resolve effectivity in a decision record before changing dependent
model values. RCS command timing does not establish delivered impulse. A scalar
engine cant does not establish the lateral rotation and jet/force geometry needed
for attitude control. Distinct hatches and flow paths remain distinct equipment.

Converter and thermal performance values require applicable measurements from primary sources,
published primary evidence or supported derivations of those measurements. Do not fill them with plausible
`chosen` defaults. A missing capacity, conductance, impedance, start condition,
flow or impulse stays `UNCONFIGURED` with a named question and resolution owner.
A decision to relax this standard belongs to the operator, not an implementing agent.

Use the evidence-gap issue template to record search results, rejected sources,
applicability and the next bounded research question. Cite frozen parent material
by name/line without editing it or making standalone tools/tests open it.
Put resolved inputs in their existing canonical YAML home and attach provenance;
do not create a second editable database of model constants in planning docs.

Closure needs the applicable source/derivation, the resulting model change and
validation of its consumer. Finding a PDF alone closes a research child issue,
not a physical-capability work package. If the required evidence cannot be found,
leave a named blocker and bring a concrete scope/model decision to the operator.
