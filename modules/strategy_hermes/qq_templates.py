"""QQ-only presentation, with a single managed template directory and no model calls."""
from hashlib import sha256
import os
from pathlib import Path
import re

MESSAGE_FORMAT = 'qq-template-v2'
PACKAGED = Path(__file__).resolve().parent / 'qq_templates'
FIELDS = {
    'research': 'title clock market leaders boards etf_candidates etf_watch etf_blocked etf_gaps stocks simulation iopv active_directions details report_id',
    'maintenance': 'slot business_date status mapping_counts mapping_version items calendar_end weight_sources pending_items research_impact task_id',
    'status': 'task_name status time result reason next_action qq_state task_id',
    'validation': 'time conclusion data_result maintenance_result research_result octop_result fix_result qq_result pending_items schedule report_id',
    'daily': 'sentiment mainline stock stock_state logic levels risk_note',
}
SLOT = re.compile(r'\{\{([a-z_]+)\}\}')
LIMITS = {'maintenance': 1400}


def validate_template(kind, raw):
    if kind not in FIELDS or not isinstance(raw, bytes) or not 0 < len(raw) <= 8192:
        raise ValueError('QQ_TEMPLATE_INVALID_SIZE_OR_KIND')
    text = raw.decode('utf-8-sig').replace('\r\n', '\n')
    lines = text.splitlines()
    if len(lines)>64 or any(len(line)>300 or any(ord(c)<32 for c in line) for line in lines):
        raise ValueError('QQ_TEMPLATE_INVALID_LINE')
    keys = SLOT.findall(text)
    if set(keys)!=set(FIELDS[kind].split()) or len(keys)!=len(set(keys)):
        raise ValueError('QQ_TEMPLATE_FIELD_MISMATCH')
    if any(c in SLOT.sub('', text) for c in '{}'):
        raise ValueError('QQ_TEMPLATE_INVALID_PLACEHOLDER')
    return text


def _read(path):
    # The configured directory is a deployment-owned, read-only bind mount.
    # Reject file symlinks and cap reads; never evaluate template contents.
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    if path.is_symlink():
        raise ValueError('QQ_TEMPLATE_SYMLINK')
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        return stream.read(8193)


def selected_template(kind, template_dir=None):
    if kind not in FIELDS:
        raise ValueError('QQ_TEMPLATE_UNKNOWN_KIND')
    configured = template_dir or os.environ.get('APP_QQ_TEMPLATE_DIR')
    fallback = False
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ValueError('QQ_TEMPLATE_DIRECTORY_NOT_ABSOLUTE')
        try:
            raw = _read(path / (kind+'.txt'))
            return validate_template(kind, raw), raw, 'managed', False
        except (OSError, ValueError, UnicodeError):
            fallback = True
    raw = _read(PACKAGED / (kind+'.txt'))
    return validate_template(kind, raw), raw, 'bundled', fallback


def _values(kind, fields):
    if not isinstance(fields, dict) or set(fields)-set(FIELDS[kind].split()):
        raise ValueError('QQ_MESSAGE_UNKNOWN_FIELDS')
    result={}
    for key in FIELDS[kind].split():
        value=fields.get(key,'')
        entries=value if isinstance(value,list) else [value]
        if len(entries)>32:
            raise ValueError('QQ_MESSAGE_TOO_MANY_ENTRIES')
        cleaned=[]
        for entry in entries:
            if not isinstance(entry,str) or len(entry)>8192:
                raise ValueError('QQ_MESSAGE_INVALID_FIELD')
            entry=entry.replace('\r\n','\n').strip()
            if any(ord(c)<32 and c!='\n' or ord(c)==127 for c in entry):
                raise ValueError('QQ_MESSAGE_CONTROL_CHARACTER')
            if entry:cleaned.append(entry)
        result[key]=cleaned
    if sum(len(v) for entries in result.values() for v in entries)>32768:
        raise ValueError('QQ_MESSAGE_TOO_LARGE')
    return result


def render_message(kind, fields, *, preview=False, template_dir=None):
    """Render validated saved facts, compacting whole cards/rows, never their meaning."""
    layout,raw,source,fallback=selected_template(kind,template_dir)
    values=_values(kind,fields)
    maximum=LIMITS.get(kind,1500)
    hint='⋯ 其余详见完整回放' if preview else '⋯ 其余详见工作台报告' if kind=='research' else '⋯ 其余详见任务记录'
    compacted=False

    def draw():
        lines=[]
        for line in layout.splitlines():
            keys=SLOT.findall(line)
            if keys and not any(values[k] for k in keys):continue
            rendered=SLOT.sub(lambda match:'\n'.join(values[match[1]]) or ('—' if len(keys)>1 else ''),line)
            if compacted and any(k in ('report_id','task_id') for k in keys):
                lines.append(hint)
            lines.append(rendered)
        text='\n'.join(lines).strip()
        return re.sub(r'\n{3,}','\n\n',text)

    # Drop whole detail cards / item lines first, preserving conditions within a card.
    for key in ('details','items','leaders','active_directions','etf_watch','boards','stocks','etf_blocked','etf_gaps','simulation','etf_candidates'):
        if len(draw())<=maximum:break
        if key not in values:continue
        if key not in ('details','items') and len(values[key])==1 and '\n' in values[key][0]:
            values[key]=values[key][0].splitlines()
        minimum=0 if key in ('details','leaders','active_directions','etf_watch') else 1
        while len(draw())>maximum and len(values[key])>minimum:
            values[key].pop();compacted=True
    # Very long individual fields remain accessible in the saved report, with an explicit pointer.
    protected={'title','clock','report_id','task_id','task_name','status','time','slot','business_date','research_impact'}
    for key in sorted(values,key=lambda k:sum(map(len,values[k])),reverse=True):
        if len(draw())<=maximum:break
        if key not in protected and sum(map(len,values[key]))>80:
            values[key]=['内容较长，详见工作台报告' if kind=='research' else '内容较长，详见任务记录']
            compacted=True
    text=draw()
    if not text or len(text)>maximum:
        raise ValueError('QQ_MESSAGE_EXCEEDS_LIMIT')
    return dict(status='READY',text=text,template=f'{MESSAGE_FORMAT}:{kind}:{source}:{sha256(raw).hexdigest()}',
                template_fallback=fallback,compacted=compacted)
