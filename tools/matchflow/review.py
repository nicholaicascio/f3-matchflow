"""Reviewed facts about one fixed-address target.

Selection used to force every target into one of three mutually exclusive lanes
(production, research, integration).  Those labels conflated unrelated hazards:
a sibling report, a repeated blocker and a prior full-unit failure have different
consequences.  This module records the facts separately and derives only the
requirements the facts justify.  `lane` remains readable on legacy prepared
batches, but it is never written for new assignments.
"""
REPORT_SCOPES = ('none', 'function', 'sibling', 'unit', 'unknown')
VERDICT_HISTORY = ('reviewed', 'unknown')
NEAR_MISS_DIFFS = 40
FACT_KEYS = {
    'history_reviewed', 'history_sources', 'prior_verdicts', 'verdict_history',
    'report_scope', 'report_evidence', 'prior_unfit', 'next_experiment',
    'integration_plan',
}
def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _legacy_facts(target):
    """Translate an already prepared lane selection once, for compatibility."""
    verdicts = list(target.get('prior_verdicts') or [])
    return dict(
        history_reviewed=target.get('history_reviewed') is True,
        history_sources=list(target.get('history_sources') or []),
        prior_verdicts=verdicts,
        verdict_history=target.get('verdict_history', 'unknown'),
        report_scope='unknown' if target.get('file_problem_reports', 0) else 'none',
        report_evidence=('legacy selection recorded only a sibling/file report count; scope not inspected'
                         if target.get('file_problem_reports', 0) else None),
        prior_unfit=bool(target.get('prior_unfit')) or target.get('lane') == 'integration' or 'unfit' in verdicts,
        next_experiment=target.get('new_hypothesis'),
        integration_plan=target.get('integration_plan'),
    )


def facts(target):
    """Return normalized review facts, rejecting an ambiguous lane/review mix."""
    if not isinstance(target, dict):
        raise ValueError('selection target must be an object')
    if 'review' in target:
        if 'lane' in target:
            raise ValueError('selection must record review facts, not a mutually exclusive lane')
        value = target['review']
        if not isinstance(value, dict):
            raise ValueError('review facts must be an object')
        return value
    if 'lane' in target:
        return _legacy_facts(target)
    raise ValueError('selection needs reviewed facts (history, report scope, prior verdicts)')


def _optional_text(value, label):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s must be substantive text or null' % label)
    return value.strip()


def normalized(target, strict=True):
    """Validate every fact type and fill optional keys for a stable snapshot.

    `strict=False` is for recording an already-prepared assignment or reporting a
    historical one: types and enums are still enforced, but a legacy assignment is
    not retroactively required to have a new plan or next experiment.
    """
    value = facts(target)
    extra = set(value) - FACT_KEYS
    if extra:
        raise ValueError('unknown review facts: ' + ', '.join(sorted(extra)))
    if value.get('history_reviewed') is not True:
        raise ValueError('review the target history before dispatch')
    sources = value.get('history_sources')
    if not isinstance(sources, list) or not sources or not all(_text(s) for s in sources):
        raise ValueError('review history and name its evidence sources before dispatch')
    verdicts = value.get('prior_verdicts')
    if (not isinstance(verdicts, list) or
            not all(isinstance(v, str) and v.strip() for v in verdicts)):
        raise ValueError('prior_verdicts must be a list of nonempty strings')
    verdicts = [v.strip() for v in verdicts]
    if value.get('verdict_history') not in VERDICT_HISTORY:
        raise ValueError('verdict_history must be reviewed or unknown')
    if value.get('report_scope') not in REPORT_SCOPES:
        raise ValueError('report_scope must be one of: ' + ', '.join(REPORT_SCOPES))
    if not isinstance(value.get('prior_unfit'), bool):
        raise ValueError('prior_unfit must be explicit true or false')
    evidence = _optional_text(value.get('report_evidence'), 'report_evidence')
    if value['report_scope'] != 'none' and evidence is None:
        raise ValueError('a report scope needs the actual report evidence or a reference')
    if target.get('open_problem_reports', 0) and value['report_scope'] in ('none', 'sibling'):
        raise ValueError('an open report on this function cannot be declared none or sibling-only')
    if target.get('file_problem_reports', 0) and value['report_scope'] == 'none' and evidence is None:
        raise ValueError('file reports need the reviewed scope or why they do not apply')
    unfit = value['prior_unfit'] or 'unfit' in verdicts
    plan = _optional_text(value.get('integration_plan'), 'integration_plan')
    if strict and unfit and plan is None:
        raise ValueError('prior unfit requires a concrete integration plan for the full-unit failure')
    next_experiment = _optional_text(value.get('next_experiment'), 'next_experiment')
    blockers = (unfit or value['report_scope'] in ('function', 'unit') or
                target.get('give_ups', 0) >= 3 or target.get('open_problem_reports', 0))
    if strict and blockers and next_experiment is None:
        raise ValueError('this history needs a concrete next_experiment, not only a lane label')
    return dict(
        history_reviewed=True,
        history_sources=[s.strip() for s in sources],
        prior_verdicts=verdicts,
        verdict_history=value['verdict_history'],
        report_scope=value['report_scope'],
        report_evidence=evidence,
        prior_unfit=unfit,
        next_experiment=next_experiment,
        integration_plan=plan,
    )


def for_report(target):
    """Facts for a report, including legacy lane assignments, without new strictness.

    A historical lane assignment may not have recorded an integration plan or a
    next experiment.  The report must not invent one, but it can still describe
    the effective facts it actually had.
    """
    if 'review' in target:
        return normalized(target, strict=False)
    value = _legacy_facts(target)
    sources = [s.strip() for s in value.get('history_sources') or [] if _text(s)]
    verdicts = [v.strip() for v in value.get('prior_verdicts') or [] if _text(v)]
    scope = value.get('report_scope')
    if scope not in REPORT_SCOPES:
        scope = 'unknown'
    evidence = value.get('report_evidence')
    if scope != 'none' and not _text(evidence):
        evidence = 'legacy assignment recorded a file report count without scope evidence'
    return dict(
        history_reviewed=bool(value.get('history_reviewed')),
        history_sources=sources or ['legacy assignment'],
        prior_verdicts=verdicts,
        verdict_history=value.get('verdict_history') if value.get('verdict_history') in VERDICT_HISTORY else 'unknown',
        report_scope=scope,
        report_evidence=evidence.strip() if _text(evidence) else None,
        prior_unfit=bool(value.get('prior_unfit')) or 'unfit' in verdicts,
        next_experiment=value.get('next_experiment').strip() if _text(value.get('next_experiment')) else None,
        integration_plan=value.get('integration_plan').strip() if _text(value.get('integration_plan')) else None,
    )


def summary(target):
    """Compact facts block used by the immutable worker prompt."""
    value = normalized(target)
    lines = [
        'history: reviewed from ' + '; '.join(value['history_sources']),
        'prior attempts: %s; open reports on function: %s; reports on source file: %s' % (
            target.get('give_ups', 'unknown'), target.get('open_problem_reports', 'unknown'),
            target.get('file_problem_reports', 'unknown')),
        'report scope: %s%s' % (value['report_scope'],
                                (' (' + value['report_evidence'] + ')') if value['report_evidence'] else ''),
        'prior verdicts: %s; verdict history: %s; prior unfit: %s' % (
            ', '.join(value['prior_verdicts']) or 'none', value['verdict_history'],
            'yes' if value['prior_unfit'] else 'no'),
    ]
    if value['next_experiment']:
        lines.append('next experiment: ' + value['next_experiment'])
    if value['integration_plan']:
        lines.append('integration plan: ' + value['integration_plan'])
    if 'lane' in target:
        lines.append('legacy batch lane label (not an active rule): ' + str(target['lane']))
    return '\n'.join(lines)
