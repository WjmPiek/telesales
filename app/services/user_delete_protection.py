"""Narrow the existing users DELETE guard without changing reset protections."""

from sqlalchemy import text

from app import db


_LEGACY_DELETE = (
    "CREATE TRIGGER protect_configuration_delete BEFORE DELETE ON public.users "
    "FOR EACH STATEMENT EXECUTE FUNCTION system_protection.protect_configuration()"
)
_SCOPED_DELETE = (
    "CREATE TRIGGER protect_configuration_delete BEFORE DELETE ON public.users "
    "FOR EACH ROW EXECUTE FUNCTION martins_protect_single_user_delete()"
)
_PROTECTED_TRUNCATE = (
    "CREATE TRIGGER protect_configuration_truncate BEFORE TRUNCATE ON public.users "
    "FOR EACH STATEMENT EXECUTE FUNCTION system_protection.protect_configuration()"
)


def ensure_scoped_user_delete_guard():
    """Allow just the requested user ID to be deleted in an authorized transaction.

    Refuse to change an unknown trigger configuration. The unconditional
    TRUNCATE guard, protections on other tables, and the Wjm account remain.
    """
    if db.engine.dialect.name != "postgresql":
        return False

    rows = db.session.execute(text("""
        SELECT tgname, pg_get_triggerdef(oid) AS definition
        FROM pg_trigger
        WHERE tgrelid = to_regclass('public.users') AND NOT tgisinternal
          AND tgname IN ('protect_configuration_delete', 'protect_configuration_truncate')
    """)).all()
    triggers = {row.tgname: row.definition for row in rows}
    if triggers.get("protect_configuration_truncate") != _PROTECTED_TRUNCATE:
        raise RuntimeError("Users TRUNCATE protection differs from the expected configuration")
    current_delete = triggers.get("protect_configuration_delete")
    if current_delete in {_SCOPED_DELETE, _SCOPED_DELETE.replace(
            "FUNCTION martins_", "FUNCTION public.martins_")}:
        return False
    if current_delete != _LEGACY_DELETE:
        raise RuntimeError("Users DELETE protection differs from the expected configuration")

    try:
        db.session.execute(text("""
            CREATE FUNCTION public.martins_protect_single_user_delete()
            RETURNS trigger LANGUAGE plpgsql AS $guard$
            DECLARE authorized_id text;
            BEGIN
                IF TG_OP <> 'DELETE' OR TG_TABLE_SCHEMA <> 'public' OR TG_TABLE_NAME <> 'users' THEN
                    RAISE EXCEPTION 'Protected configuration: deletion is blocked.';
                END IF;
                IF lower(coalesce(OLD.email, '')) = 'wjm@martinsdirect.com' THEN
                    RAISE EXCEPTION 'The protected Super Admin account cannot be deleted.';
                END IF;
                authorized_id := current_setting('martins.authorized_user_delete_id', true);
                IF authorized_id IS NULL OR authorized_id !~ '^[1-9][0-9]*$' THEN
                    RAISE EXCEPTION 'Protected configuration: user deletion is not authorized.';
                END IF;
                IF authorized_id::bigint <> OLD.id THEN
                    RAISE EXCEPTION 'Protected configuration: only the authorized user can be deleted.';
                END IF;
                RETURN OLD;
            END
            $guard$
        """))
        db.session.execute(text("DROP TRIGGER protect_configuration_delete ON public.users"))
        db.session.execute(text("""
            CREATE TRIGGER protect_configuration_delete
            BEFORE DELETE ON public.users
            FOR EACH ROW EXECUTE FUNCTION public.martins_protect_single_user_delete()
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return True
