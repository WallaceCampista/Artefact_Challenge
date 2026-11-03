# 02 · Store information and opening hours around a holiday

> **Scenario:** Address and hours come from the fixed store card in the prompt (no tool needed). On a Sunday the customer asks about tomorrow, which is a national holiday (Finados): the holiday calendar is computed in code and given to the model as context.
>
> Recorded with `python -m emporio examples` · model `qwen2.5-7b-instruct` · embeddings `text-embedding-qwen3-embedding-0.6b` · clock frozen at 2026-11-01T10:00:00 (America/Campo_Grande) · answers are not edited.

**Cliente:** Oi! Qual o endereço da loja?

**Tom:** Olá! Nosso endereço é Rua 14 de Maio, 3200, Centro, Campo Grande/MS, CEP 79202-333. Posso lhe ajudar com algo mais?

<sub>No tool needed · 2.6 s</sub>

---

**Cliente:** Posso passar aí amanhã de manhã para conhecer os ukuleles?

**Tom:** A loja está fechada amanhã de manhã por ser um feriado. Ela reabre na terça-feira, 03/11 às 09:00. Você pode nos visitar então para conhecer nossos ukuleles. Posso lhe ajudar com algo mais?

<details><summary>Tools used: consultar_horario · 3.8 s</summary>

`consultar_horario({"quando": "amanha"})`

```json
{"dia": "amanhã (segunda-feira, 02/11/2026)", "situacao": "FECHADA (feriado: Finados)", "reabre": "terça-feira, 03/11 às 09:00"}
```

</details>

