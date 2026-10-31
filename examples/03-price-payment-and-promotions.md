# 03 · Price lookup, payment conditions and promotions

> **Scenario:** Price of a specific model (challenge suggestion), PIX discount and installments computed from the policy rules, then a question about promotions: promotional prices must show the original price and the discount, and the PIX discount does not stack (policy 6.2).
>
> Recorded with `python -m emporio examples` · model `qwen2.5-7b-instruct` · embeddings `text-embedding-qwen3-embedding-0.6b` · clock frozen at 2026-11-03T10:30:00 (America/Campo_Grande) · answers are not edited.

**Cliente:** Quanto custa o Takamine GD20?

**Tom:** O Takamine GD20 Dreadnought Natural custa R$ 2.199,00 no nosso site. Se você pagar com PIX, terá um desconto de 5%, totalizando R$ 2.089,05.  
  
Você pode optar também pelo cartão de crédito em até 12x sem juros, pagando R$ 183,25 por parcela. A opção de débito ou boleto bancário tem o valor integral do produto.  
  
Qual forma de pagamento você preferiria?

<details><summary>Tools used: detalhes_produto · 11.3 s</summary>

`detalhes_produto({"produto": "Takamine GD20"})`

```json
{"id": 95, "nome": "Takamine GD20 Dreadnought Natural", "marca": "Takamine", "categoria": "Violões", "preco_de_tabela": "R$ 2.199,00", "preco_final": "R$ 2.199,00", "disponibilidade": "disponível", "estoque": 5, "descricao": "Violão dreadnought Takamine com tampo sólido em cedro. Excelente para fingerstyle com timbre quente e definido.", "especificacoes": {"tampo": "Cedro Sólido", "fundo e laterais": "Sapele", "braço": "Mogno", "cordas": "aço", "escala": "650mm", "eletrônica (captação)": "não", "cor": "Natural"}, "promocoes_encerradas": ["Oferta Especial (12%)"], "pagamento": {"pix": "R$ 2.089,05 (5% de desconto)", "cartao_de_credito": "até 12x de R$ 183,25 sem juros", "debito_ou_boleto": "R$ 2.199,00", "pode_combinar_pix_e_cartao": true}, "observacao": "As promoções em promocoes_encerradas NÃO estão valendo; se o cliente perguntar, diga que encerraram e informe o preço atual (regra …
```

</details>

---

**Cliente:** Se eu pagar no PIX fica quanto? E dá pra parcelar em quantas vezes?

**Tom:** Se você pagar com PIX, terá um desconto de 5%, totalizando R$ 2.089,05.  
  
Quanto à forma de pagamento, você pode optar por cartão de crédito em até 12x sem juros, ou débito/boleto bancário no valor integral do produto.  
  
Qual opção você prefere?

<details><summary>Tools used: detalhes_produto · 7.5 s</summary>

`detalhes_produto({"produto": "Takamine GD20 Dreadnought Natural"})`

```json
{"id": 95, "nome": "Takamine GD20 Dreadnought Natural", "marca": "Takamine", "categoria": "Violões", "preco_de_tabela": "R$ 2.199,00", "preco_final": "R$ 2.199,00", "disponibilidade": "disponível", "estoque": 5, "descricao": "Violão dreadnought Takamine com tampo sólido em cedro. Excelente para fingerstyle com timbre quente e definido.", "especificacoes": {"tampo": "Cedro Sólido", "fundo e laterais": "Sapele", "braço": "Mogno", "cordas": "aço", "escala": "650mm", "eletrônica (captação)": "não", "cor": "Natural"}, "promocoes_encerradas": ["Oferta Especial (12%)"], "pagamento": {"pix": "R$ 2.089,05 (5% de desconto)", "cartao_de_credito": "até 12x de R$ 183,25 sem juros", "debito_ou_boleto": "R$ 2.199,00", "pode_combinar_pix_e_cartao": true}, "observacao": "As promoções em promocoes_encerradas NÃO estão valendo; se o cliente perguntar, diga que encerraram e informe o preço atual (regra …
```

</details>

---

**Cliente:** Tem algum violão em promoção?

**Tom:** Sim, temos dois violões em promoção:  
  
1. **Crafter HT-100 Folk Aço Natural**: Com desconto de 18%, sai por R$ 1.967,18 (preço original: R$ 2.399,00).  
2. **Taylor 110e Dreadnought Natural**: Com desconto de 8%, sai por R$ 5.519,08 (preço original: R$ 5.999,00).  
  
Ambos estão disponíveis para compra. Se você pagar com PIX, terá um desconto adicional de 5%, mas só em produtos sem promoção.  
  
Qual desses modelos te interessa?

<details><summary>Tools used: buscar_produtos · 23.2 s</summary>

`buscar_produtos({"categoria": "Violões", "apenas_promocoes": true})`

```json
{"total_encontrado": 2, "produtos": [{"id": 90, "nome": "Crafter HT-100 Folk Aço Natural", "preco": "R$ 1.967,18", "disponibilidade": "disponível (últimas 3 unidades)", "promocao": "18% de desconto (Desconto Exclusivo): de R$ 2.399,00 por R$ 1.967,18"}, {"id": 94, "nome": "Taylor 110e Dreadnought Natural", "preco": "R$ 5.519,08", "disponibilidade": "disponível", "promocao": "8% de desconto (Desconto Exclusivo): de R$ 5.999,00 por R$ 5.519,08"}], "observacao": "2 destes produtos estão em promoção (campo promocao); os demais estão a preço de tabela. PIX dá 5% de desconto só em produtos sem promoção."}
```

</details>

