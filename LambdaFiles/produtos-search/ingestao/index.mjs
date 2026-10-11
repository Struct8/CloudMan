// Lambda de INGESTAO / indexacao de produtos.
//
// Acionada pela fila SQS via Event Source Mapping (batch). Para cada mensagem,
// faz UPSERT do produto no Aurora pelo RDS Data API. O search_vector (tsvector)
// e mantido por trigger/coluna gerada no banco (ver schema.sql), entao aqui
// so gravamos os campos do produto.
//
// Como o ESM esta com function_response_types = ["ReportBatchItemFailures"],
// devolvemos os ids das mensagens que falharam para o SQS reentregar apenas elas.

import {
  RDSDataClient,
  ExecuteStatementCommand,
} from "@aws-sdk/client-rds-data";

const REGION = process.env.REGION || process.env.AWS_REGION;
const CLUSTER_ARN = process.env.AWS_RDS_CLUSTER_ARN_0;
const SECRET_ARN = process.env.AWS_RDS_CLUSTER_SECRET_ARN_0;
const DB_NAME = process.env.AWS_RDS_CLUSTER_DB_NAME_0 || "produtos";

const client = new RDSDataClient({ region: REGION });

const UPSERT_SQL = `
  INSERT INTO produtos (id, nome, marca, categoria, descricao, preco, specs)
  VALUES (:id, :nome, :marca, :categoria, :descricao, :preco, CAST(:specs AS jsonb))
  ON CONFLICT (id) DO UPDATE SET
    nome      = EXCLUDED.nome,
    marca     = EXCLUDED.marca,
    categoria = EXCLUDED.categoria,
    descricao = EXCLUDED.descricao,
    preco     = EXCLUDED.preco,
    specs     = EXCLUDED.specs,
    atualizado_em = now();
`;

async function upsertProduto(p) {
  const parameters = [
    { name: "id", value: { stringValue: String(p.id) } },
    { name: "nome", value: { stringValue: p.nome ?? "" } },
    { name: "marca", value: { stringValue: p.marca ?? "" } },
    { name: "categoria", value: { stringValue: p.categoria ?? "" } },
    { name: "descricao", value: { stringValue: p.descricao ?? "" } },
    {
      name: "preco",
      value:
        p.preco == null
          ? { isNull: true }
          : { doubleValue: Number(p.preco) },
    },
    { name: "specs", value: { stringValue: JSON.stringify(p.specs ?? {}) } },
  ];

  await client.send(
    new ExecuteStatementCommand({
      resourceArn: CLUSTER_ARN,
      secretArn: SECRET_ARN,
      database: DB_NAME,
      sql: UPSERT_SQL,
      parameters,
    })
  );
}

export const handler = async (event) => {
  const records = (event && event.Records) || [];
  const batchItemFailures = [];

  for (const record of records) {
    try {
      const produto = JSON.parse(record.body);
      if (!produto || produto.id == null) {
        // Mensagem invalida: loga e descarta (nao reentrega para nao travar a fila).
        console.warn("mensagem sem id, descartada", record.messageId);
        continue;
      }
      await upsertProduto(produto);
    } catch (err) {
      console.error("falha ao processar mensagem", record.messageId, err);
      batchItemFailures.push({ itemIdentifier: record.messageId });
    }
  }

  return { batchItemFailures };
};
