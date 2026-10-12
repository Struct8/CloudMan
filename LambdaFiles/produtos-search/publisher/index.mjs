// Lambda PUBLISHER: recebe um produto via POST /produtos (API Gateway HTTP API)
// e publica na fila SQS, que alimenta a Lambda de ingestao. Mantem a escrita
// desacoplada do banco (o site responde rapido; a ingestao processa em batch).
//
// A URL da fila vem da variavel de ambiente injetada pelo Struct8 a partir da
// conexao Lambda -> SQS (AWS_SQS_QUEUE_URL_* / endpoint). Veja o fallback abaixo.

import { SQSClient, SendMessageCommand } from "@aws-sdk/client-sqs";

const REGION = process.env.REGION || process.env.AWS_REGION;
const ACCOUNT = process.env.ACCOUNT;
// O Struct8 injeta o NOME da fila conectada (AWS_SQS_QUEUE_NAME_0), nao a URL.
// A URL de uma fila SQS e deterministica: https://sqs.<regiao>.amazonaws.com/<account>/<nome>.
// Montamos a partir de REGION/ACCOUNT/NAME; aceitamos tambem uma URL explicita por env.
const QUEUE_NAME = process.env.AWS_SQS_QUEUE_NAME_0;
const QUEUE_URL =
  process.env.AWS_SQS_QUEUE_URL_0 ||
  process.env.QUEUE_URL ||
  (REGION && ACCOUNT && QUEUE_NAME
    ? `https://sqs.${REGION}.amazonaws.com/${ACCOUNT}/${QUEUE_NAME}`
    : undefined);

const client = new SQSClient({ region: REGION });

function response(statusCode, body) {
  return {
    statusCode,
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  };
}

// Gera um id simples e url-safe quando o produto nao traz um.
function genId() {
  return "p_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

export const handler = async (event) => {
  if (!QUEUE_URL) {
    console.error("QUEUE_URL ausente no ambiente");
    return response(500, { error: "fila nao configurada" });
  }

  let input;
  try {
    input = JSON.parse(event?.body || "{}");
  } catch {
    return response(400, { error: "JSON invalido no corpo" });
  }

  const nome = (input.nome || "").trim();
  if (!nome) {
    return response(400, { error: "campo 'nome' obrigatorio" });
  }

  const produto = {
    id: (input.id && String(input.id).trim()) || genId(),
    nome,
    marca: input.marca ?? "",
    categoria: input.categoria ?? "",
    descricao: input.descricao ?? "",
    preco: input.preco == null || input.preco === "" ? null : Number(input.preco),
    imagem_url: input.imagem_url ?? null,
    specs: input.specs && typeof input.specs === "object" ? input.specs : {},
  };

  try {
    await client.send(
      new SendMessageCommand({
        QueueUrl: QUEUE_URL,
        MessageBody: JSON.stringify(produto),
      })
    );
    // 202: aceito para processamento assincrono (a ingestao grava no banco).
    return response(202, { ok: true, id: produto.id });
  } catch (err) {
    console.error("falha ao publicar na fila", err);
    return response(500, { error: "falha ao enfileirar produto" });
  }
};
