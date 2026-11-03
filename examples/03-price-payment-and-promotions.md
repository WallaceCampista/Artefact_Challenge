# 03 · Price lookup, payment conditions and promotions

> **Scenario:** Price of a specific model (challenge suggestion), PIX discount and installments computed from the policy rules, then a question about promotions: promotional prices must show the original price and the discount, and the PIX discount does not stack (policy 6.2).
>
> Recorded with `python -m emporio examples` · model `qwen2.5-7b-instruct` · embeddings `text-embedding-qwen3-embedding-0.6b` · clock frozen at 2026-11-03T10:30:00 (America/Campo_Grande) · answers are not edited.

**Cliente:** Quanto custa o Takamine GD20?

**Tom:** O Takamine GD20 Dreadnought Natural custa R$ 2.199,00 no cartão de crédito. Com PIX, você paga R$ 2.089,05 com 5% de desconto.  
  
Você pode parcelar em até 12x sem juros no cartão, pagando R$ 183,25 por parcela. Ou escolher outras opções como 2 a 12 vezes com valores diferentes.  
  
Posso lhe ajudar com algo mais?

<details><summary>Tools used: detalhes_produto · 7.8 s</summary>

`detalhes_produto({"produto": "Takamine GD20"})`

```json
{"id": 95, "nome": "Takamine GD20 Dreadnought Natural", "marca": "Takamine", "categoria": "Violões", "preco_de_tabela": "R$ 2.199,00", "preco_final": "R$ 2.199,00", "disponibilidade": "disponível", "estoque": 5, "descricao": "Violão dreadnought Takamine com tampo sólido em cedro. Excelente para fingerstyle com timbre quente e definido.", "especificacoes": {"tampo": "Cedro Sólido", "fundo e laterais": "Sapele", "braço": "Mogno", "cordas": "aço", "escala": "650mm", "eletrônica (captação)": "não", "cor": "Natural"}, "promocoes_encerradas": ["Oferta Especial (12%)"], "pagamento": {"pix": "R$ 2.089,05 (5% de desconto)", "cartao_de_credito": "até 12x de R$ 183,25 sem juros", "parcelas_sem_juros": "2x de R$ 1.099,50; 3x de R$ 733,00; 4x de R$ 549,75; 5x de R$ 439,80; 6x de R$ 366,50; 7x de R$ 314,14; 8x de R$ 274,88; 9x de R$ 244,33; 10x de R$ 219,90; 11x de R$ 199,91; 12x de R$ 183,25", …
```

</details>

---

**Cliente:** Se eu pagar no PIX fica quanto? E dá pra parcelar em quantas vezes?

**Tom:** Com PIX, você pagará R$ 2.089,05 com 5% de desconto.  
  
Você pode parcelar em até 12x no cartão sem juros:  
- 2x de R$ 1.099,50  
- 3x de R$ 733,00  
- 4x de R$ 549,75  
- 5x de R$ 439,80  
- 6x de R$ 366,50  
- 7x de R$ 314,14  
- 8x de R$ 274,88  
- 9x de R$ 244,33  
- 10x de R$ 219,90  
- 11x de R$ 199,91  
- 12x de R$ 183,25  
  
Posso lhe ajudar com algo mais?

<details><summary>Tools used: detalhes_produto · 11.2 s</summary>

`detalhes_produto({"produto": "Takamine GD20 Dreadnought Natural"})`

```json
{"id": 95, "nome": "Takamine GD20 Dreadnought Natural", "marca": "Takamine", "categoria": "Violões", "preco_de_tabela": "R$ 2.199,00", "preco_final": "R$ 2.199,00", "disponibilidade": "disponível", "estoque": 5, "descricao": "Violão dreadnought Takamine com tampo sólido em cedro. Excelente para fingerstyle com timbre quente e definido.", "especificacoes": {"tampo": "Cedro Sólido", "fundo e laterais": "Sapele", "braço": "Mogno", "cordas": "aço", "escala": "650mm", "eletrônica (captação)": "não", "cor": "Natural"}, "promocoes_encerradas": ["Oferta Especial (12%)"], "pagamento": {"pix": "R$ 2.089,05 (5% de desconto)", "cartao_de_credito": "até 12x de R$ 183,25 sem juros", "parcelas_sem_juros": "2x de R$ 1.099,50; 3x de R$ 733,00; 4x de R$ 549,75; 5x de R$ 439,80; 6x de R$ 366,50; 7x de R$ 314,14; 8x de R$ 274,88; 9x de R$ 244,33; 10x de R$ 219,90; 11x de R$ 199,91; 12x de R$ 183,25", …
```

</details>

---

**Cliente:** Tem algum violão em promoção?

**Tom:** Aqui estão as opções em promoção:  
  
1. **Crafter HT-100 Folk Aço Natural** - De R$ 2.399,00 por R$ 1.967,18 (18% de desconto)  
2. **Taylor 110e Dreadnought Natural** - De R$ 5.999,00 por R$ 5.519,08 (8% de desconto)  
  
Posso lhe ajudar com algo mais?

<details><summary>Tools used: buscar_produtos · 6.7 s</summary>

`buscar_produtos({"categoria": "Violões", "apenas_promocoes": true})`

```json
{"total_encontrado": 2, "produtos": [{"id": 90, "nome": "Crafter HT-100 Folk Aço Natural", "preco": "R$ 1.967,18", "disponibilidade": "disponível (últimas 3 unidades)", "promocao": "18% de desconto (Desconto Exclusivo): de R$ 2.399,00 por R$ 1.967,18"}, {"id": 94, "nome": "Taylor 110e Dreadnought Natural", "preco": "R$ 5.519,08", "disponibilidade": "disponível", "promocao": "8% de desconto (Desconto Exclusivo): de R$ 5.999,00 por R$ 5.519,08"}], "observacao": "2 destes produtos estão em promoção (campo promocao); os demais estão a preço de tabela. PIX dá 5% de desconto só em produtos sem promoção."}
```

</details>

