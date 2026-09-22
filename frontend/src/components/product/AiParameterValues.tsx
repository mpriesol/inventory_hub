import React from 'react';
import { useTranslation } from 'react-i18next';
import { AiContent, AiParameter } from '../../api/aiContent';

type Fact = { id: number; name: string; variant_attributes: { name: string; value: string }[] };
type Value = AiContent['parameters'][number];

export function missingRequiredParameterValues(values: Value[], registry: AiParameter[], facts: Fact[]): Value[] {
  return registry.filter(parameter => parameter.required).flatMap(parameter =>
    (parameter.scope === 'parent' ? [null] : facts.map(fact => fact.id))
      .filter(id => !values.some(value => value.name === parameter.name && value.product_id === id && value.values.some(v => v.trim())))
      .map(product_id => ({ name: parameter.name, product_id, values: [] })));
}

export function AiParameterValues({ values, registry, facts, disabled, onChange }: {
  values: Value[]; registry: AiParameter[]; facts: Fact[]; disabled: boolean; onChange: (values: Value[]) => void;
}) {
  const { t } = useTranslation();
  const definitions = new Map(registry.map(parameter => [parameter.name, parameter]));
  const missing = missingRequiredParameterValues(values, registry, facts);
  const rows = [...values, ...missing.filter(slot => !values.some(value => value.name === slot.name && value.product_id === slot.product_id))];
  const change = (index: number, patch: Partial<Value>) => onChange(rows.map((value, i) => i === index ? { ...value, ...patch } : value));
  const variantName = (fact: Fact) => `${fact.name}${fact.variant_attributes.length ? ` · ${fact.variant_attributes.map(a => a.value).join(' / ')}` : ''}`;
  return <div className="ai-parameter-values">
    {registry.length === 0 && <p className="ai-notice">{t('ai.noRegisteredParameters')}</p>}
    {registry.some(parameter => parameter.required) && <p>{t('ai.parameterEditor.requiredHelp')}</p>}
    {missing.length > 0 && <p className="ai-notice">{t('ai.parameterEditor.missing')}: {[...new Set(missing.map(parameter => parameter.name))].join(', ')}</p>}
    <div className="ai-scroll"><table><thead><tr><th>{t('ai.parameterEditor.name')}</th><th>{t('ai.parameterScope')}</th><th>{t('ai.parameterValues')}</th><th /></tr></thead><tbody>{rows.map((value, index) => {
      const definition = definitions.get(value.name);
      const variant = facts.find(f => f.id === value.product_id);
      const invalidScope = definition && (definition.scope === 'parent' ? value.product_id !== null : !variant);
      const isMissing = missing.some(slot => slot.name === value.name && slot.product_id === value.product_id);
      const choices = [...new Set([...(definition?.values || []), ...value.values])];
      return <tr key={index} className={isMissing ? 'ai-parameter-missing' : undefined}>
        <td><select aria-label={`${t('ai.parameterEditor.name')} ${index + 1}`} disabled={disabled || isMissing} value={value.name} onChange={e => change(index, { name: e.target.value })}>
          <option value="">{t('ai.parameterEditor.choose')}</option>
          {value.name && !definition && <option value={value.name}>{value.name} · {t('ai.parameterEditor.unregistered')}</option>}
          {registry.map(parameter => <option key={parameter.name} value={parameter.name}>{parameter.name}{parameter.required ? ` · ${t('ai.required')}` : ''}</option>)}
        </select>{isMissing && <small className="ai-parameter-missing-label">{t('ai.parameterEditor.missingValue')}</small>}{value.name && !definition && <small>{t('ai.parameterEditor.unknownHelp')}</small>}{definition?.instructions && <small>{definition.instructions}</small>}</td>
        <td><select aria-label={`${t('ai.parameterScope')} ${index + 1}`} disabled={disabled || isMissing} value={value.product_id === null ? 'parent' : String(value.product_id)} onChange={e => change(index, { product_id: e.target.value === 'parent' ? null : Number(e.target.value) })}>
          <option value="parent" disabled={definition?.scope === 'variant'}>{t(definition?.scope === 'variant' ? 'ai.parameterEditor.chooseVariant' : 'ai.parent')}</option>
          {definition?.scope !== 'parent' && facts.map(fact => <option value={fact.id} key={fact.id}>{variantName(fact)}</option>)}
          {value.product_id !== null && (definition?.scope === 'parent' || !variant) && <option value={value.product_id}>{variant ? variantName(variant) : t('ai.parameterEditor.unknownVariant', {id:value.product_id})}</option>}
        </select>{invalidScope && <small>{t(`ai.parameterEditor.${definition.scope === 'parent' ? 'needsParent' : 'needsVariant'}`)}</small>}</td>
        <td>{definition?.values.length ? <fieldset aria-label={`${value.name} · ${variant ? variantName(variant) : t('ai.parent')}`} aria-invalid={isMissing || undefined}>{choices.map(choice => <label className="ai-check" key={choice}><input type="checkbox" disabled={disabled} checked={value.values.includes(choice)} onChange={e => change(index, { values: e.target.checked ? [...value.values, choice] : value.values.filter(v => v !== choice) })} />{choice}{!definition.values.includes(choice) && ` · ${t('ai.parameterEditor.invalidValue')}`}</label>)}</fieldset>
          : <label><span>{t('ai.parameterEditor.onePerLine')}{definition?.unit ? ` · ${definition.unit}` : ''}</span><textarea aria-label={`${t('ai.parameterValues')} ${index + 1}`} aria-invalid={isMissing || undefined} disabled={disabled} value={value.values.join('\n')} onChange={e => change(index, { values: e.target.value === '' ? [] : e.target.value.split('\n') })} /></label>}</td>
        <td>{!isMissing && <button type="button" disabled={disabled} onClick={() => onChange(rows.filter((_, i) => i !== index))}>{t('ai.removeDraftEntry')}</button>}</td>
      </tr>;
    })}</tbody></table></div>
    <button type="button" disabled={disabled || !registry.length} onClick={() => onChange([...rows, { name: '', product_id: null, values: [] }])}>{t('ai.addParameter')}</button>
  </div>;
}
