import React from 'react';
import { useTranslation } from 'react-i18next';
import { AiContent, AiParameter } from '../../api/aiContent';

type Fact = { id: number; name: string; variant_attributes: { name: string; value: string }[] };
type Value = AiContent['parameters'][number];

export function AiParameterValues({ values, registry, facts, disabled, onChange }: {
  values: Value[]; registry: AiParameter[]; facts: Fact[]; disabled: boolean; onChange: (values: Value[]) => void;
}) {
  const { t } = useTranslation();
  const definitions = new Map(registry.map(parameter => [parameter.name, parameter]));
  const missing = registry.filter(parameter => parameter.required && (parameter.scope === 'parent' ? [null] : facts.map(f => f.id)).some(id =>
    !values.some(value => value.name === parameter.name && value.product_id === id && value.values.some(v => v.trim()))));
  const change = (index: number, patch: Partial<Value>) => onChange(values.map((value, i) => i === index ? { ...value, ...patch } : value));
  const variantName = (fact: Fact) => `${fact.name}${fact.variant_attributes.length ? ` · ${fact.variant_attributes.map(a => a.value).join(' / ')}` : ''}`;
  return <div>
    {registry.length === 0 && <p className="ai-notice">{t('ai.noRegisteredParameters')}</p>}
    {missing.length > 0 && <p className="ai-notice">{t('ai.parameterEditor.missing')}: {missing.map(parameter => parameter.name).join(', ')}</p>}
    <div className="ai-scroll"><table><thead><tr><th>{t('ai.parameterEditor.name')}</th><th>{t('ai.parameterScope')}</th><th>{t('ai.parameterValues')}</th><th /></tr></thead><tbody>{values.map((value, index) => {
      const definition = definitions.get(value.name);
      const variant = facts.find(f => f.id === value.product_id);
      const invalidScope = definition && (definition.scope === 'parent' ? value.product_id !== null : !variant);
      const choices = [...new Set([...(definition?.values || []), ...value.values])];
      return <tr key={index}>
        <td><select aria-label={`${t('ai.parameterEditor.name')} ${index + 1}`} disabled={disabled} value={value.name} onChange={e => change(index, { name: e.target.value })}>
          <option value="">{t('ai.parameterEditor.choose')}</option>
          {value.name && !definition && <option value={value.name}>{value.name} · {t('ai.parameterEditor.unregistered')}</option>}
          {registry.map(parameter => <option key={parameter.name} value={parameter.name}>{parameter.name}{parameter.required ? ` · ${t('ai.required')}` : ''}</option>)}
        </select>{value.name && !definition && <small>{t('ai.parameterEditor.unknownHelp')}</small>}{definition?.instructions && <small>{definition.instructions}</small>}</td>
        <td><select aria-label={`${t('ai.parameterScope')} ${index + 1}`} disabled={disabled} value={value.product_id === null ? 'parent' : String(value.product_id)} onChange={e => change(index, { product_id: e.target.value === 'parent' ? null : Number(e.target.value) })}>
          <option value="parent" disabled={definition?.scope === 'variant'}>{t(definition?.scope === 'variant' ? 'ai.parameterEditor.chooseVariant' : 'ai.parent')}</option>
          {definition?.scope !== 'parent' && facts.map(fact => <option value={fact.id} key={fact.id}>{variantName(fact)}</option>)}
          {value.product_id !== null && (definition?.scope === 'parent' || !variant) && <option value={value.product_id}>{variant ? variantName(variant) : t('ai.parameterEditor.unknownVariant', {id:value.product_id})}</option>}
        </select>{invalidScope && <small>{t(`ai.parameterEditor.${definition.scope === 'parent' ? 'needsParent' : 'needsVariant'}`)}</small>}</td>
        <td>{definition?.values.length ? <div>{choices.map(choice => <label className="ai-check" key={choice}><input type="checkbox" disabled={disabled} checked={value.values.includes(choice)} onChange={e => change(index, { values: e.target.checked ? [...value.values, choice] : value.values.filter(v => v !== choice) })} />{choice}{!definition.values.includes(choice) && ` · ${t('ai.parameterEditor.invalidValue')}`}</label>)}</div>
          : <label><span>{t('ai.parameterEditor.onePerLine')}{definition?.unit ? ` · ${definition.unit}` : ''}</span><textarea aria-label={`${t('ai.parameterValues')} ${index + 1}`} disabled={disabled} value={value.values.join('\n')} onChange={e => change(index, { values: e.target.value === '' ? [] : e.target.value.split('\n') })} /></label>}</td>
        <td><button type="button" disabled={disabled} onClick={() => onChange(values.filter((_, i) => i !== index))}>{t('ai.removeDraftEntry')}</button></td>
      </tr>;
    })}</tbody></table></div>
    <button type="button" disabled={disabled || !registry.length} onClick={() => onChange([...values, { name: '', product_id: null, values: [] }])}>{t('ai.addParameter')}</button>
  </div>;
}
