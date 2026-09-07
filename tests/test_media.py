from app.config import Settings
from app.repositories.store import DemoStore
from app.schemas import MediaUpdate
from app.services.media import MediaService


def media_service(tmp_path):
    settings = Settings(
        data_mode="demo",
        demo_data_path=tmp_path / "store.json",
        data_raw_path=tmp_path / "raw",
        media_inbox_path=tmp_path / "inbox",
        media_library_path=tmp_path / "library",
        media_sync_legacy_files=True,
    )
    service = MediaService(DemoStore(settings.demo_data_path), settings)
    service.workspace = tmp_path
    return service


def test_sync_catalogs_existing_files_without_duplicates(tmp_path):
    service = media_service(tmp_path)
    contract = tmp_path / "CONTRATO DE LOCAÇÃO.pdf"
    contract.write_bytes(b"contract contents")
    receipt = tmp_path / "WhatsApp Image 2026-08-05 at 14.32.36.jpeg"
    receipt.write_bytes(b"receipt image contents")

    first_sync = service.sync_workspace()
    second_sync = service.sync_workspace()
    records = service.list()

    assert first_sync["added"] == 2
    assert second_sync["skipped"] == 2
    assert len(records) == 2
    contract_record = next(record for record in records if record["kind"] == "document")
    receipt_record = next(record for record in records if record["kind"] == "image")
    assert contract_record["metadata"]["valid_until"] == "2025-03-20"
    assert receipt_record["metadata"]["supplier"] == "Nechio Comércio"
    assert service.content(receipt_record) == b"receipt image contents"


def test_upload_deduplicates_by_content_and_keeps_organization(tmp_path):
    service = media_service(tmp_path)

    original = service.upload("nota-fornecedor.pdf", b"same file", "Compras")
    duplicate = service.upload("nota-copiada.pdf", b"same file", "Financeiro")
    updated = service.update(
        original["id"],
        MediaUpdate(category="Compras", tags=["Fornecedor", "agosto", "fornecedor"], entity_type="inventory", entity_id="acai"),
    )

    assert original["id"] == duplicate["id"]
    assert len(service.list()) == 1
    assert updated["tags"] == ["fornecedor", "agosto"]
    assert updated["entity_id"] == "acai"


def test_deleting_an_uploaded_file_removes_it_from_the_shelf(tmp_path):
    service = media_service(tmp_path)
    registro = service.upload("nota.pdf", b"conteudo da nota")

    resultado = service.delete(registro["id"])

    assert resultado["status"] == "excluido"
    assert service.get(registro["id"]) is None
    assert service.local_path(registro) is None, "a cópia do sistema também sai"


def test_a_synced_file_does_not_come_back_on_the_next_sync(tmp_path):
    """Excluir arquivo sincronizado precisa valer.

    O original do usuário não é apagado — o ERP catalogou o arquivo, não virou
    dono dele. Mas sem registrar a dispensa, a próxima sincronização traria
    tudo de volta e a exclusão pareceria não ter funcionado.
    """

    service = media_service(tmp_path)
    (tmp_path / "planilha antiga.xlsx").write_bytes(b"conteudo qualquer")
    service.sync_workspace()
    registro = next(row for row in service.list() if row["original_name"] == "planilha antiga.xlsx")

    service.delete(registro["id"])
    service.sync_workspace()

    assert not any(row["original_name"] == "planilha antiga.xlsx" for row in service.list())
    assert (tmp_path / "planilha antiga.xlsx").exists(), "o arquivo original do usuário não pode ser apagado"


def test_media_partial_link_update_uses_existing_type(tmp_path):
    service = media_service(tmp_path)
    record = service.upload('nota.pdf', b'pdf')
    service.update(record['id'], MediaUpdate(entity_type='order', entity_id='one'))
    updated = service.update(record['id'], MediaUpdate(entity_id='two'))
    assert updated['entity_type'] == 'order'
    assert updated['entity_id'] == 'two'
