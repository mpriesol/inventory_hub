"""Pure AI/draft lineage parsing; never call AI public/recovery readers."""
from inventory_hub.catalog_types import CatalogProduct
from inventory_hub.product_import_types import ImportValues
from inventory_hub.services import product_dependency_scope as s


CONTEXT = set('supplier feed_key product_ids run_id shop target code name image source_digest facts technical_sources_version use_ai rules_version resolved category_profile requested_category_profile category_profile_source options research sale_price_overrides model reasoning_effort verbosity estimate_usd staging_id source_kind staging_automation_version category_policy category_selection category_candidates category_registry category_source category_profile_only category_current category_state category_error category_attempts classification_catalog classification_mode approval update_preview update_result archived retry_import source_job_id update_only source_snapshot source_parameters_loaded update_source_digest supplier_availability'.split())
CONTEXT.update({'staging_applied_digest', 'staging_auto_publication'})
CONTEXT.update({'prompt_version', 'max_output_tokens', 'classification_max_output_tokens'})
FACT = set('id code manufacturer_code eans name brand group_name description safety_information parameters variant_attributes technical_tables source_documents source_warnings source_kind existing_seo_title existing_meta_description variant_identity'.split())
UPDATE = set('id state target expires_at fields identity language before after payload shop_stock supplier_availability availability_basis category_codes_by_id system_category_codes sent_at observed_at attempt_id request_id error'.split())


def native_identity(proof, identity, namespace, path):
    s.obj(identity, {'product_id', 'code', 'ean', 'variants'}, ('product_id', 'code', 'variants'), path)
    proof.parent_write(namespace, identity['code'], identity['product_id'], path)
    if identity.get('ean'):
        proof.eans([identity['ean']], path + '/ean')
    for i, leaf in enumerate(s.array(identity['variants'], path + '/variants')):
        if isinstance(leaf, list) and len(leaf) == 3:
            leaf_code, vid, ean = leaf
        elif isinstance(leaf, dict):
            s.obj(leaf, {'code', 'variant_id', 'ean'}, ('code', 'variant_id'), path + '/variants')
            leaf_code, vid, ean = leaf['code'], leaf['variant_id'], leaf.get('ean')
        else:
            raise s.Unverified('invalid_native_family_identity', path)
        proof.leaf(leaf_code, namespace, f'{path}/variants/{i}/code')
        proof.native_leaf(namespace, identity['product_id'], vid, f'{path}/variants/{i}')
        if ean:
            proof.eans([ean], f'{path}/variants/{i}/ean')


def inspect(proof, raw, drafts, catalog_sources):
    if raw.get('kind') == 'rules':
        from inventory_hub.services.product_dependency_rule_scope import inspect as inspect_rule
        return inspect_rule(proof, raw)
    ctx = s.obj(raw.get('context'), CONTEXT, ('shop', 'code'), '/context')
    if raw.get('kind') != 'product':
        # Other job kinds use different executors and schemas. A new
        # parser, not a job-status guess, is needed to exclude their intent.
        raise s.Unverified('unsupported_ai_job_kind', '/kind')
    namespace = s.shop(ctx['shop'])
    staging = ctx.get('staging_id')
    source_kind = ctx.get('source_kind', 'catalog')
    if bool(staging) != (source_kind == 'import_draft'):
        raise s.Unverified('ai_staging_kind_mismatch', '/context/staging_id')
    facts = s.array(ctx.get('facts'), '/context/facts', nonempty=True)
    by_id = {}
    for i, fact in enumerate(facts):
        s.obj(fact, FACT, ('id', 'code', 'eans'), f'/context/facts/{i}')
        sid = s.positive(fact['id'])
        if sid in by_id:
            raise s.Unverified('duplicate_ai_source_id', '/context/facts')
        by_id[sid] = fact
        proof.eans(fact['eans'], f'/context/facts/{i}/eans')
        proof.leaf(fact['code'], namespace, f'/context/facts/{i}/code', canonical=source_kind != 'shop')
    if staging:
        if not isinstance(staging, str) or not s.UUID32.fullmatch(staging) or staging not in drafts:
            raise s.Unverified('ai_draft_link_missing', '/context/staging_id')
        draft = drafts[staging]
        if draft.get('shop') != namespace or draft.get('supplier') != ctx.get('supplier'):
            raise s.Unverified('ai_draft_namespace_changed', '/context/staging_id')
        rows = s.draft(proof, draft, '/linked_draft/document')
        linked = [row for row in rows if row.get('ai_job_id') == raw['id']]
        ids = s.array(ctx.get('product_ids'), '/context/product_ids', nonempty=True)
        ids = [s.positive(value) for value in ids]
        if len(ids) != len(set(ids)) or set(ids) != set(by_id) or set(ids) != {row['id'] for row in linked}:
            raise s.Unverified('ai_draft_selection_changed', '/context/product_ids')
        for row in linked:
            baseline = s.model(ImportValues, row.get('ai_baseline'), '/linked_draft/ai_baseline')
            fact = by_id[row['id']]
            if (row['source'].get('id') != row['id'] or fact['code'] != baseline.code
                    or fact['eans'] != baseline.eans
                    or (fact.get('brand') or '') != baseline.brand
                    or (fact.get('manufacturer_code') or '') != baseline.manufacturer_code
                    or fact.get('variant_attributes', []) != [v.model_dump() for v in baseline.variant_attributes]):
                raise s.Unverified('ai_draft_baseline_changed', '/linked_draft/ai_baseline')
        if ctx.get('update_only') or ctx.get('update_preview'):
            raise s.Unverified('staged_ai_existing_update_mixed', '/context')
        version = ctx.get('staging_automation_version')
        if version not in (None, 1):
            raise s.Unverified('unknown_staging_automation', '/context/staging_automation_version')
        # advance_ai may save/preview/queue the WHOLE draft after this job.
        proof.linked_drafts.add(staging)
        proof.context('/context/code', 'proven_staged_family_container_full_draft_checked')
    elif source_kind == 'shop':
        if ctx.get('update_only') is not True or ctx.get('source_parameters_loaded') is not True:
            raise s.Unverified('shop_ai_source_incomplete', '/context')
        snapshot = s.obj(ctx.get('source_snapshot'),
            {'product_id', 'code', 'ean', 'brand', 'descriptions', 'parameters', 'variants'},
            ('product_id', 'code', 'variants'), '/context/source_snapshot')
        if snapshot['code'] != ctx['code']:
            raise s.Unverified('shop_ai_source_code_changed', '/context/code')
        native_identity(proof, {key: snapshot.get(key) for key in ('product_id', 'code', 'ean', 'variants')},
                        namespace, '/context/source_snapshot')
        proof.parent_write(namespace, ctx['code'], snapshot['product_id'], '/context/code')
    elif source_kind == 'catalog':
        ids = [s.positive(value) for value in s.array(ctx.get('product_ids'), '/context/product_ids', nonempty=True)]
        if len(ids) != len(set(ids)) or set(ids) != set(by_id):
            raise s.Unverified('catalog_ai_source_selection_changed', '/context/product_ids')
        supplier = s.shop(ctx.get('supplier'))
        for sid in ids:
            source = catalog_sources.get((supplier, ctx.get('feed_key', 'products'), sid))
            if source is None:
                raise s.Unverified('catalog_ai_source_missing', '/context/product_ids')
            source = s.model(CatalogProduct, source, '/catalog_source')
            fact = by_id[sid]
            if source.id != sid or source.shop_code != fact['code'] or source.eans != fact['eans']:
                raise s.Unverified('catalog_ai_frozen_source_changed', '/context/facts')
            proof.importer(source, '/catalog_source/' + str(sid), supplier=supplier, targets=[namespace])
        if ctx.get('update_only'):
            proof.parent_write(namespace, ctx['code'], None, '/context/code')
        else:
            proof.context('/context/code', 'create_only_catalog_family')
    else:
        raise s.Unverified('unknown_ai_source_kind', '/context/source_kind')
    update = ctx.get('update_preview')
    if update:
        s.obj(update, UPDATE, ('identity', 'payload', 'state'), '/context/update_preview')
        native_identity(proof, update['identity'], namespace, '/context/update_preview/identity')
        s.payload(proof, update['payload'], namespace, '/context/update_preview/payload', parent_update=True,
                  pid=update['identity']['product_id'])
    if raw.get('preview_id'):
        if not s.UUID32.fullmatch(raw['preview_id']):
            raise s.Unverified('ai_publication_id_invalid', '/preview_id')
        document = drafts[staging]['document'] if staging else {}
        previews = document.get('publications')
        aggregate = document.get('publication') or {}
        if isinstance(previews, dict) and len(previews) > 1 and aggregate.get('preview_id') == raw['preview_id']:
            proof.context('/preview_id', 'validated_multishop_aggregate_use_authoritative_publications')
        else:
            proof.publications.add((namespace, raw['preview_id']))
    if raw.get('output'):
        from inventory_hub.ai_content_types import Content
        output = s.model(Content, raw['output'], '/output')
        if any(parameter.product_id is not None and parameter.product_id not in by_id for parameter in output.parameters):
            raise s.Unverified('ai_output_source_id_outside_selection', '/output/parameters')
