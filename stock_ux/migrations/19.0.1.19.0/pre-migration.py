import logging

_logger = logging.getLogger(__name__)

OBSOLETE_FIELDS = (
    "qty_multiple_over_max",
    "stock_orderpoint_allow_multiple_over_max",
    "book_required",
    "book_id",
    "next_number",
    "voucher_ids",
    "vouchers",
    "with_vouchers",
    "lines_per_voucher",
    "voucher_required",
    "voucher_number",
    "voucher_number_unique",
    "next_voucher_number",
    "sequence_to",
    "autoprinted",
)

EXPLICIT_XMLIDS = (
    ("stock_ux", "view_warehouse_orderpoint_form_multiple_over_max"),
    ("stock_ux", "procurement_group_form_view"),
    ("stock_voucher", "vpicktree"),
    ("stock_voucher", "view_picking_form"),
    ("stock_voucher", "view_stock_book_form"),
    ("stock_voucher", "view_stock_book_tree"),
    ("stock_voucher", "view_stock_picking_voucher_tree"),
    ("stock_voucher", "view_stock_picking_voucher_form"),
    ("stock_voucher_ux", "view_picking_form"),
    ("stock_voucher_ux", "view_stock_book_form_ux"),
    ("stock_voucher_ux", "report_delivery_document"),
    ("stock_currency_valuation", "stock_valuation_layer_tree"),
    ("stock_currency_valuation", "stock_valuation_layer_form"),
)

OBSOLETE_MODULES = (
    "stock_voucher",
    "stock_voucher_ux",
    "stock_voucher_ux_iot",
    "stock_currency_valuation_recompute",
)


def _migrar_remitos_stock_voucher(cr):
    """
    Migra los remitos historicos de stock_voucher hacia stock_picking.l10n_ar_delivery_guide_number
    y hace un backup permanente de la tabla stock_picking_voucher antes de cualquier limpieza.
    """
    cr.execute("ALTER TABLE stock_picking ADD COLUMN IF NOT EXISTS l10n_ar_delivery_guide_number VARCHAR")

    cr.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
             WHERE table_schema = 'public' 
               AND table_name = 'stock_picking_voucher'
        )
    """)
    if cr.fetchone()[0]:
        cr.execute("""
            CREATE TABLE IF NOT EXISTS stock_picking_voucher_backup AS 
            SELECT * FROM stock_picking_voucher
        """)
        _logger.info("stock_ux pre-migration: tabla stock_picking_voucher_backup asegurada")

        cr.execute("""
            UPDATE stock_picking p
               SET l10n_ar_delivery_guide_number = sub.remitos
              FROM (
                  SELECT picking_id, string_agg(name, ', ' ORDER BY id) AS remitos
                    FROM stock_picking_voucher
                   WHERE name IS NOT NULL AND trim(name) != ''
                   GROUP BY picking_id
              ) sub
             WHERE p.id = sub.picking_id
               AND (p.l10n_ar_delivery_guide_number IS NULL OR trim(p.l10n_ar_delivery_guide_number) = '')
        """)
        _logger.info("stock_ux pre-migration: %s remitos migrados desde stock_picking_voucher a l10n_ar_delivery_guide_number", cr.rowcount)

    cr.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.columns 
             WHERE table_name = 'stock_picking' 
               AND column_name = 'vouchers'
        )
    """)
    if cr.fetchone()[0]:
        cr.execute("""
            UPDATE stock_picking
               SET l10n_ar_delivery_guide_number = vouchers
             WHERE (l10n_ar_delivery_guide_number IS NULL OR trim(l10n_ar_delivery_guide_number) = '')
               AND vouchers IS NOT NULL AND trim(vouchers) != ''
        """)
        _logger.info("stock_ux pre-migration: %s remitos migrados desde stock_picking.vouchers a l10n_ar_delivery_guide_number", cr.rowcount)


def migrate(cr, version):
    _logger.info("stock_ux pre-migration: iniciando limpieza y migracion de remitos (v19)")
    _migrar_remitos_stock_voucher(cr)

    # 1. Semillas por XMLID de ir_model_data
    cr.execute(
        """
        SELECT res_id FROM ir_model_data
         WHERE model = 'ir.ui.view'
           AND (
               (module, name) IN %s
               OR module IN %s
           )
           AND res_id IS NOT NULL
        """,
        (EXPLICIT_XMLIDS, OBSOLETE_MODULES),
    )
    xml_seeds = {row[0] for row in cr.fetchall()}

    # 2. Semillas por campos obsoletos en arch_db
    pattern = r"\y(" + "|".join(OBSOLETE_FIELDS) + r")\y"
    cr.execute(
        """
        SELECT id FROM ir_ui_view
         WHERE type != 'qweb'
           AND arch_db::text ~ %s
        """,
        (pattern,),
    )
    field_seeds = {row[0] for row in cr.fetchall()}

    all_seeds = xml_seeds | field_seeds

    if all_seeds:
        # 3. Arrastrar vistas hijas recursivamente
        cr.execute(
            """
            WITH RECURSIVE arbol(id) AS (
                SELECT id FROM ir_ui_view WHERE id IN %s
                UNION
                SELECT h.id FROM ir_ui_view h JOIN arbol a ON h.inherit_id = a.id
            )
            SELECT id FROM arbol
            """,
            (tuple(all_seeds),),
        )
        pendientes = {row[0] for row in cr.fetchall()}
        _logger.info(
            "stock_ux pre-migration: borrando %s vistas obsoletas (semillas=%s)",
            len(pendientes),
            len(all_seeds),
        )

        total_borradas = 0
        while pendientes:
            cr.execute(
                """
                DELETE FROM ir_ui_view
                 WHERE id IN %s
                   AND id NOT IN (SELECT inherit_id FROM ir_ui_view WHERE inherit_id IS NOT NULL)
             RETURNING id
                """,
                (tuple(pendientes),),
            )
            borradas = {row[0] for row in cr.fetchall()}
            if not borradas:
                # Si hubiera referencias circulares, romper inherit_id primero
                cr.execute(
                    "UPDATE ir_ui_view SET inherit_id = NULL WHERE id IN %s",
                    (tuple(pendientes),),
                )
                cr.execute(
                    "DELETE FROM ir_ui_view WHERE id IN %s",
                    (tuple(pendientes),),
                )
                total_borradas += len(pendientes)
                break
            pendientes -= borradas
            total_borradas += len(borradas)

        _logger.info("stock_ux pre-migration: %s vistas eliminadas", total_borradas)

    # 4. Limpieza de ir_model_data
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE model = 'ir.ui.view'
           AND (
               res_id NOT IN (SELECT id FROM ir_ui_view)
               OR res_id IS NULL
               OR (module, name) IN %s
               OR module IN %s
           )
        """,
        (EXPLICIT_XMLIDS, OBSOLETE_MODULES),
    )

    # 5. Limpieza de campos en ir_model_fields
    cr.execute(
        """
        DELETE FROM ir_model_fields
         WHERE (model = 'stock.warehouse.orderpoint' AND name = 'qty_multiple_over_max')
            OR (model = 'res.company' AND name = 'stock_orderpoint_allow_multiple_over_max')
            OR (model = 'res.config.settings' AND name = 'stock_orderpoint_allow_multiple_over_max')
        """
    )

    # 6. Limpieza de acciones obsoletas si existen
    cr.execute(
        """
        SELECT id FROM ir_act_window
         WHERE COALESCE(domain, '') ~ %s
            OR COALESCE(context, '') ~ %s
        """,
        (pattern, pattern),
    )
    act_ids = [r[0] for r in cr.fetchall()]
    if act_ids:
        cr.execute(
            """
            DELETE FROM ir_ui_menu
             WHERE action IN (
                 SELECT 'ir.actions.act_window,' || id FROM ir_act_window WHERE id IN %s
             )
            """,
            (tuple(act_ids),),
        )
        cr.execute("DELETE FROM ir_act_window WHERE id IN %s", (tuple(act_ids),))
        _logger.info("stock_ux pre-migration: %s acciones de ventana eliminadas", len(act_ids))

    _logger.info("stock_ux pre-migration: finalizado exitosamente")
