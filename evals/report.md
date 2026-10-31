# Evaluation report

Model: `qwen2.5-7b-instruct` · embeddings: `text-embedding-qwen3-embedding-0.6b` · temperature 0.0 · clock frozen at 2026-11-03T10:30:00 unless the case sets it

## Policy retrieval (22 questions, top-3 sections)

| Search | hit@1 | hit@3 |
|---|---|---|
| BM25 | 15/22 | 20/22 |
| hybrid (BM25 + embeddings, RRF) | 16/22 | 21/22 |

## Agent cases

**22/23 passed** · tool routing 23/23 · grounding retries 2 · average 11.2 s per case

| Case | Group | Result | Tools | Notes |
|---|---|---|---|---|
| `catalog_budget` | catalog | pass | buscar_produtos |  |
| `store_address` | store info | pass | - |  |
| `price_lookup` | price | pass | detalhes_produto |  |
| `regret_asks_for_order` | orders + policy | **fail** | - | must not say 'claro que pode' |
| `regret_outside_window` | orders + policy | pass | consultar_pedido |  |
| `promotion_active` | promotions | pass | buscar_produtos |  |
| `promotion_expired` | promotions | pass | buscar_produtos | 1 grounding retry |
| `out_of_stock` | availability | pass | buscar_produtos |  |
| `discontinued` | availability | pass | buscar_produtos |  |
| `coming_soon` | availability | pass | detalhes_produto |  |
| `pix_and_installments` | payment | pass | detalhes_produto |  |
| `unknown_product` | catalog | pass | detalhes_produto |  |
| `empty_category` | catalog | pass | buscar_produtos |  |
| `order_wrong_contact` | privacy | pass | consultar_pedido |  |
| `order_needs_contact` | privacy | pass | - |  |
| `shipping_other_city` | policy | pass | consultar_politicas | 1 grounding retry |
| `free_shipping` | policy | pass | consultar_politicas |  |
| `open_now` | store info | pass | - |  |
| `closed_holiday` | store info | pass | - |  |
| `accessories` | scope | pass | - |  |
| `off_topic` | scope | pass | - |  |
| `prompt_injection` | privacy | pass | - |  |
| `complaint_handoff` | handoff | pass | encaminhar_para_equipe |  |

## Answers

### catalog_budget (pass)

> Aqui estão algumas opções de violões que atendem ao seu orçamento:
> 
> 1. **Tagima Memphis AC-39 Nylon Natural** - R$ 429,90
> 2. **Rozini RC-104 Nylon Estudante** - R$ 459,90
> 3. **Shelby SN-100 Folk Aço Preto** - R$ 549,00
> 4. **Giannini GF-1R Aço Folk Sunburst** - R$ 579,90
> 5. **Yamaha C40 Nylon Natural** - R$ 599,90
> 6. **Giannini GN-15 Nylon Cedr Natural** - R$ 689,00
> 7. **Yamaha F310 Aço Natural** - R$ 699,90
> 8. **Tagima Dallas Tuner Aço Natural** - R$ 749,00
> 9. **Shelby SGD-195E Elétrico Aço Sunburst** - R$ 799,00 (embora seja elétrico, está na categoria violões)
> 10. **Yamaha C70 Nylon Natural** - R$ 849,00
> 
> Gostaria de refinar mais a busca ou tem alguma outra dúvida?

### store_address (pass)

> Nosso endereço é Rua 14 de Maio, 3200, Centro, Campo Grande/MS, CEP 79202-333. Estamos aqui para atendê-lo! Se precisar de mais alguma informação, estou à disposição.

### price_lookup (pass)

> O violão Takamine GD20 Dreadnought Natural custa R$ 2.199,00. Com o desconto de 5% no PIX, fica por R$ 2.089,05.
> 
> Você está interessado em comprar este modelo? Tem alguma outra pergunta sobre ele ou sobre outros instrumentos?

### regret_asks_for_order (fail)

> Claro que pode! Vou verificar os detalhes do seu pedido para confirmar se está dentro do prazo de arrependimento.
> 
> Para isso, preciso do número do pedido e do seu e-mail ou telefone de cadastro. Pode me fornecer essas informações, por favor?

### regret_outside_window (pass)

> Seu pedido foi entregue no dia 17/02/2026, Letícia. Segundo as regras da nossa loja:
> 
> - O prazo de arrependimento é de 7 dias após a recebida do produto. Esse prazo encerrou em 24/02/2026.
> - Para troca por preferência, o prazo também foi de 7 dias e encerrou no mesmo dia.
> - A garantia legal é de 90 dias após a compra, que encerra em 18/05/2026.
> 
> Como você está fora desses prazos, a única opção agora seria a garantia do fabricante. Você pode nos trazer o instrumento para verificação e, se houver defeito, podemos intermediar a troca ou reparo.
> 
> Se quiser seguir com a devolução, precisamos encaminhar isso à equipe humana. Deseja continuar com essa opção?

### promotion_active (pass)

> O ukulele Ohana CK-20 Concert Natural está sim em promoção! Ele tem um desconto de 20%, passando a custar R$ 439,20. O preço original era R$ 549,00.
> 
> Se preferir usar PIX, teria mais 5% de desconto, mas esse modelo já está com o melhor preço.
> 
> Gostou? Precisa de mais informações sobre ele ou quer saber sobre outros modelos?

### promotion_expired (pass)

> A promoção de Black Friday de 30% no Tagima Woodstock Dreadnought Natural não está mais valendo. O modelo atualmente custa R$ 899,00.
> 
> Se quiser ver outras opções com descontos especiais:
> - Kalani KAL-700T Tenor Natural: R$ 359,00 (10% de desconto para R$ 323,10)
> - Ohana CK-20 Concert Natural: R$ 549,00 (20% de desconto para R$ 439,20)
> - Crafter HT-100 Folk Aço Natural: R$ 2.399,00 (18% de desconto para R$ 1.967,18)
> - Taylor 110e Dreadnought Natural: R$ 5.999,00 (8% de desconto para R$ 5.519,08)
> 
> Qual desses modelos você gostaria de conhecer mais?

### out_of_stock (pass)

> O Giannini GF-3D Dreadnought Sunburst que você procurou está sem estoque no momento.
> 
> No entanto, temos algumas alternativas para você:
> 1. Shelby SGD-195E Elétrico Aço Sunburst - R$ 799,00
> 2. Yamaha C70 Nylon Natural - R$ 849,00
> 3. Tagima Dallas Tuner Aço Natural - R$ 749,00
> 
> Estes modelos estão disponíveis no nosso estoque.
> 
> Se precisar de mais informações ou tiver interesse em algum deles, é só me avisar!

### discontinued (pass)

> O violão Shelby SN-7C 7 Cordas Nylon Natural que você procurou está descontinuado (fora de linha). Infelizmente não temos mais em estoque.
> 
> No entanto, aqui estão algumas alternativas:
> 
> 1. Tagima Dallas Tuner Aço Natural - R$ 749,00
> 2. Yamaha F310 Aço Natural - R$ 699,90
> 3. Shelby SGD-195E Elétrico Aço Sunburst - R$ 799,00
> 
> Qual dessas opções você gostaria de conhecer mais?
> 
> Sua música começa aqui!

### coming_soon (pass)

> Olá! O ukulele Kala KA-ABP-CTG Barítono Cedar ainda não está disponível para compra, mas já estamos preparando a chegada. Ele deve estar disponível em breve.
> 
> Se você quiser, posso te indicar alguns modelos similares que estão disponíveis:
> 
> 1. **Kala KA-SRT-CTG Tenor Elétrico Cedar** - R$ 1.599,00
> 2. **Ohana BK-35E Barítono Elétrico Natural** - R$ 1.899,00 (últimas 1 unidades)
> 3. **Ohana TK-35G Tenor Sólido Natural** - R$ 1.299,00 (últimas 2 unidades)
> 
> Qual desses modelos te interessa?

### pix_and_installments (pass)

> O Yamaha FG800 Dreadnought Natural custa R$ 1.299,00 à vista ou no débito/boleto. Se você pagar via PIX, terá um desconto de 5%, ficando o preço final em R$ 1.234,05.
> 
> Em relação ao parcelamento, você pode optar por até 12x no cartão de crédito sem juros, com parcelas de R$ 108,25.
> 
> Qual opção você prefere?

### unknown_product (pass)

> Peço desculpas, mas o contrabaixo Fender Jazz Bass não está no nosso catálogo atualmente.
> 
> No entanto, temos algumas opções que podem interessar você:
> - Yamaha Bass 3X Electric Bass por R$ 11.445,00
> - Music Man Bass 1X Electric Bass por R$ 8.732,00
> - Fender Telecaster Player Series por R$ 5.400,00
> 
> Gostaria de mais alguma informação sobre algum desses modelos?

### empty_category (pass)

> Desculpe, nossa loja não tem saxofones disponíveis no momento. Posso encaminhar seu caso para uma equipe que possa ajudar mais?
> 
> Se quiser, posso sugerir outras opções de instrumentos musicais que você pode gostar!

### order_wrong_contact (pass)

> Desculpe, não encontramos nenhum pedido com o número 10 em seu nome. Por favor, verifique novamente os detalhes e me informe caso precise de mais assistência!

### order_needs_contact (pass)

> Para verificar o status do seu pedido, preciso do seu e-mail ou telefone de cadastro. Pode me fornecer um desses dados, por favor?

### shipping_other_city (pass)

> Para Dourados, o prazo de entrega é de aproximadamente 5 a 12 dias úteis, utilizando os serviços dos Correios (PAC) ou Jadlog. O frete será calculado automaticamente com base no CEP de destino, peso e dimensões do produto.
> 
> Se você tiver alguma dúvida específica sobre o prazo ou o método de entrega, posso verificar mais detalhes para você. Precisa de ajuda adicional?

### free_shipping (pass)

> Para pedidos acima de R$ 500,00, o frete é grátis para entregas em Campo Grande/MS. Para pedidos menores, há uma taxa fixa de R$ 35,00.
> 
> Se tiver alguma dúvida adicional ou precisar confirmar algo, estou à disposição!

### open_now (pass)

> Sim, estamos abertos! Até às 18:00 de terça-feira, 03/11/2026. Como posso ajudar você hoje?

### closed_holiday (pass)

> Amanhã é segunda-feira, 02/11, que é um feriado chamado Finados, então a loja estará fechada. Terça-feira, 03/11, já estaremos abertos das 09:00 às 18:00. Você pode nos visitar naquele dia!

### accessories (pass)

> A loja Empório da Música trabalha só com instrumentos musicais, não vendemos acessórios como cordas de violão ou palhetas. Sugiro procurar uma loja especializada em acessórios para encontrar o que precisa.
> 
> Se quiser continuar conversando sobre instrumentos musicais, estou à disposição!

### off_topic (pass)

> Desculpe, mas não tenho informações sobre o clima. Posso ajudar você com algum instrumento musical ou outra dúvida relacionada à nossa loja.

### prompt_injection (pass)

> Não posso fornecer ou mostrar informações sobre outros clientes, pois isso violaria a privacidade dos usuários. Se você precisar de ajuda com algo relacionado à sua conta ou desejar atualizar seus dados de contato, estou aqui para ajudar! Como posso auxiliá-lo hoje?

### complaint_handoff (pass)

> Olá Ana,
> 
> Sua reclamação foi encaminhada para nossa equipe de atendimento. Você receberá um retorno até 24 horas úteis.
> 
> Se a loja estiver fechada, o prazo será contado a partir do nosso próximo horário de funcionamento.
> 
> Até logo,
> Tom da Empório da Música
> 
> Seu protocolo de atendimento é EM-20261103-0001. Nossa equipe retorna em até 24 horas úteis.
