"""Validated Kyutai delivery preferences and bounded text sections."""
from __future__ import annotations
import math
import re
import config

def defaults():
    return {'speed':float(getattr(config,'LIVE_VOICE_KYUTAI_SPEED',.9)),
            'whisper_mix':float(getattr(config,'LIVE_VOICE_KYUTAI_WHISPER_MIX',0)),
            'pause_ms':int(getattr(config,'LIVE_VOICE_KYUTAI_PAUSE_MS',180)),
            'guidance':float(getattr(config,'LIVE_VOICE_KYUTAI_GUIDANCE',2))}

def clean(value=None):
    if value is None:value={}
    if not isinstance(value,dict):raise ValueError('Delivery settings must be an object.')
    result=defaults()
    for name,low,high in [('speed',.75,1.1),('whisper_mix',0,1),('pause_ms',0,600),('guidance',1,4)]:
        try:number=float(value.get(name,result[name]))
        except (TypeError,ValueError) as e:raise ValueError('Delivery values must be numbers.') from e
        if not math.isfinite(number):raise ValueError('Delivery values must be finite.')
        result[name]=max(low,min(high,number))
    result['pause_ms']=int(result['pause_ms'])
    if result['guidance'] not in [1,1.5,2,2.5,3,3.5,4]:raise ValueError('Voice guidance uses half-step values from 1 to 4.')
    return result

def live():
    """Parlor replies use the original voice and bypass offline tempo work."""
    return clean({'speed':1,'whisper_mix':0})

def sections(text,cap=300,max_words=42):
    """Sentence/clause boundaries; shorter first section for responsive first audio."""
    words=re.findall(r'\S+',text);parts=[];pending=[];count=0
    for word in words:
        if len(word)>cap:raise ValueError('Speech contains an unbroken token longer than a safe synthesis section.')
        limit=min(cap,150) if not parts else cap
        while pending and (count+len(word)+1>limit or len(pending)>=max_words):
            # If a sentence/clause boundary was already available, cut there.
            boundary=max((i+1 for i,w in enumerate(pending) if re.search(r'[.!?;,][\"\u201d\u2019]*$',w)),default=0)
            if boundary>=max(2,len(pending)//3):
                parts.append(' '.join(pending[:boundary]));pending=pending[boundary:];count=len(' '.join(pending))
            else:parts.append(' '.join(pending));pending=[];count=0
            limit=cap
        pending.append(word);count+=len(word)+bool(count)
    if pending:parts.append(' '.join(pending))
    return parts
