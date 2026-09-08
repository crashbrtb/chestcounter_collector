# Total Battle Chest Collector

Coleta automaticamente os baús do clã no Total Battle e grava cada um no banco
MySQL/MariaDB. A interface web de consulta continua sendo a
[chestcounter](https://github.com/crashbrtb/chestcounter).

A versão 2.0 mudou as bases da aplicação: o jogo agora roda no **Chrome
controlado por CDP** em vez do aplicativo desktop, cada **conta tem login e
senha próprios** com seus perfis, e não existe mais coordenada escrita à mão —
tudo é calibrado clicando na tela e configurado por uma interface.

---

## Instalação

1. Baixe os arquivos para uma pasta (por exemplo `C:\chestcounter`).
2. Rode `install.bat` — cria o ambiente virtual e instala tudo.
3. Duplo clique em `Configurar.bat` e siga os quatro passos da aba **Execução**.

O `install.bat` aproveita as credenciais de banco de um `position.cfg` da versão
antiga, se houver: os perfis já aparecem cadastrados, faltando só o login da
conta.

### Tesseract (recomendado)

O RapidOCR vem pelo `pip`, mas o Tesseract é um programa à parte — e ele é o
motor **principal**, o mais rápido dos dois. Sem ele a coleta ainda funciona (cai
no RapidOCR), porém mais lenta e sem acentos.
Instale o [Tesseract para Windows](https://github.com/UB-Mannheim/tesseract/wiki)
com o idioma **Português** marcado.

---

## Uso

Duplo clique, sem prompt de comando:

| Arquivo | O que faz |
|---|---|
| **`Configurar.bat`** | Abre a interface: contas, perfis, bancos e todos os parâmetros |
| **`Calibrar.bat`** | Abre direto o assistente de calibração |
| `run.bat` | Coleta os baús. **É o que o Agendador de Tarefas deve chamar.** |

Os dois primeiros são atalhos para o `run.bat`, que também aceita
`run.bat config`, `run.bat calibrar` e `run.bat verificar` (confere configuração
e calibração sem coletar) para quem preferir a linha de comando.

Código de saída (visível em *Último resultado da execução* no agendador):
`0` tudo coletado · `1` alguma falha · `2` configuração ou calibração incompleta ·
`3` cancelado.

**Para cancelar uma execução, segure ESC** por um instante — de qualquer janela, mesmo com o
jogo em foco. A interface também tem um botão *Parar*. O cancelamento interrompe até as
esperas longas, e o que já foi coletado permanece gravado. O ESC que o próprio coletor envia
ao jogo passa por CDP e não toca no teclado físico, então não cancela nada.

### Agendamento

Aponte o Agendador de Tarefas do Windows para o `run.bat`. Não há mais nada para
configurar no `.bat`: **toda** a execução (módulo, tentativas, tempos, nível e
retenção de log) está em `config/config.json`, editável pela interface.

O log é escrito pelo próprio Python em
`execution_logs/collector_AAAA-MM-DD.log`, em UTF-8, com limpeza automática dos
antigos. Redirecionar a saída do `.bat` — como era antes — perdia acentos e
cortava o log quando o processo morria.

---

## Contas e perfis

```
Conta  (e-mail + senha do totalbattle.com)
  └── Perfil (cidade)  → banco de dados próprio
  └── Perfil (cidade)  → banco de dados próprio
```

A coleta respeita essa ordem: entra na conta e percorre os perfis dela antes de
passar para a próxima.

**A sessão do navegador é preservada, nunca apagada.** Limpar cookies faz o jogo
tratar o navegador como um aparelho novo e mandar um código de verificação por
e-mail — que uma execução agendada de madrugada não tem como responder. Por isso
cada execução apenas pergunta à página onde ela está: já autenticada, ou na tela
de login. Fazer login virou exceção, não rotina.

Com **mais de uma conta**, cada uma precisa do seu próprio *Perfil do navegador*
(uma pasta de dados só dela, preenchida em Contas e perfis): assim cada conta
mantém a própria sessão já verificada, e o navegador é reiniciado nessa pasta ao
trocar de conta. Se duas contas dividirem o mesmo perfil, a execução **para antes
de começar** e explica o motivo — a segunda conta rodaria dentro da sessão da
primeira e gravaria no banco errado.

**Um perfil sem banco de dados é ignorado**, com aviso no log: não haveria onde
registrar o que fosse coletado.

### Como o login é feito

O formulário do totalbattle.com **já está no HTML quando a página abre, porém
fechado** — só aparece depois de clicar em *Login*. Por isso o coletor primeiro
procura o botão que abre o formulário, clica, espera os campos ficarem visíveis
e só então digita, com eventos reais de teclado e mouse.

O que essa página em particular exigiu:

- Estar sem campo de senha visível **não** significa estar logado — a página
  deslogada também não mostra nenhum. O que indica sessão aberta é não haver
  *nem* campo de senha visível *nem* botão de login na tela.
- O botão que abre o login **é uma `div`**, não um `<button>` — a busca cobre
  qualquer elemento clicável e fica com o mais interno, senão um container
  inteiro passaria por botão.
- A página mostra o **cadastro e o login ao mesmo tempo**, e são 8 campos de
  e-mail no total: o primeiro é o do cadastro. Os campos são procurados apenas
  dentro da caixa que contém o campo de senha, senão o e-mail iria para o
  cadastro e a senha para o login.
- São descartados os botões de login social (Google, Facebook, VK…) e os
  caminhos alternativos que também falam em login — *"Log in with a code"*,
  *"Forgot password"* —, e entre os candidatos vence o rótulo mais curto:
  *"Log in"* é o botão e *"Log in to claim your reward"* é uma frase que apenas
  o contém.

Se a detecção errar, os cinco seletores CSS da seção **Login** permitem fixar na
mão o botão que abre o formulário, os campos, o botão de enviar e um elemento
que confirme o login.

---

## Calibração

O assistente (`Calibrar.bat`) tira uma foto da página pelo CDP e você marca
os controles **clicando na foto**, com uma lupa acompanhando o cursor. Assim a
posição gravada já é a coordenada da página: nada depende de onde a janela está,
nem da escala do Windows.

A captura é corrigida pela **escala do Windows**: o Chrome renderiza o
screenshot no `devicePixelRatio` (a 125%, uma página de 1536 vira imagem de
1920), mas o clique usa pixel CSS. A captura é pedida já compensada, então o
pixel que você clica na imagem **é** o pixel que o clique atinge — sem isso todo
ponto sai 1,25x grande e nenhum clique acerta.

- **ponto** — um clique (botão do clã, aba de presentes…)
- **área** — um retângulo (onde procurar texto ou um botão)
- **área com referência** — o retângulo *e* um recorte da imagem, procurado
  depois na tela; é como o botão "Abrir" é encontrado mesmo mudando de lugar

**Calibre com a janela no tamanho em que o coletor vai rodar.** Cada passo guarda
o tamanho de página em que foi gravado; se a janela mudar de tamanho no meio da
calibração, os passos anteriores continuam válidos, mas o assistente avisa se
algum ficou fora da página — um clique fora da página não atinge nada e parece
um botão que ignorou o clique.

O botão **🔎 Testar este passo** exercita o passo no jogo de verdade e **mede** o
resultado: no clique, compara a tela antes e depois e diz *"cliquei em (x,y) e a
tela mudou 13%"* ou *"a tela NÃO mudou: o clique caiu no vazio"*; na área, mostra
o que o OCR leu; na imagem de referência, se encontrou e com que semelhança.

A **lista de perfis com barra de rolagem** é tratada: o coletor rola até o topo,
procura o nome, rola para baixo e procura de novo, repetindo até a lista parar de
mudar — então uma conta com muitos perfis é percorrida inteira. Se o clique no
menu de perfis não mudar a tela, o log diz isso explicitamente, em vez de culpar
o OCR por não achar a lista.

Cada passo pode ser refeito sozinho, sem repetir a sequência. Se a janela do
navegador tiver outro tamanho no dia da execução, as coordenadas são reescaladas
e as imagens procuradas em várias escalas — a precisão cai, mas a execução não
quebra. Os dois últimos passos (loja) são opcionais.

---

## OCR

A queixa que originou esta versão: nomes estrangeiros e acentuados vinham
errados, e nome errado é baú creditado ao jogador errado.

Medindo os motores em texto renderizado como o do jogo (claro sobre fundo
escuro, letra pequena):

| Motor | Letras certas | Acentos certos | Exemplo do erro |
|---|---|---|---|
| Tesseract | 6/8 | 3/8 | `Şükrü Öztürk` → `Sukriú Oztiirk` |
| RapidOCR | 7/8 | 0/8 | `Aurélio Gonçalves` → `Aurelio Goncalves` |
| **Os dois juntos** | **7/8** | **3/8** | — |

Nenhum dos dois resolve sozinho: o Tesseract preserva acento em português mas
inventa letras em nome turco ou nórdico; o RapidOCR acerta as letras mas seu
dicionário não tem acento nenhum. O padrão (`auto`) usa os dois — **letras do
RapidOCR, acentos do Tesseract**, e só em letras sobre as quais os dois já
concordam. Nenhum motor inventa um acento que não existe; ambos apenas deixam de
ver, o que é o que torna essa regra segura.

O Tesseract também devolve o **espaçamento**: o RapidOCR cola palavras
(`ShadowChest`, `Level35epicCrypt`) porque detecta a frase inteira como um bloco,
enquanto o Tesseract separa como o coletor antigo separava. Quando os dois
concordam nas letras, vale a forma espaçada — é o que mantém os nomes novos
comparáveis com os anos de registros já no banco.

Também ajuda antes do motor: o recorte é renderizado **pelo próprio Chrome** na
escala configurada (`ocr.capture_scale`), então o motor vê letras realmente
maiores, não uma ampliação borrada de um print pequeno.

### Qual motor roda quando

O **Tesseract vai na frente** porque é o mais rápido, e o RapidOCR só é chamado
quando a confiança do Tesseract cai — medido, leituras corretas marcam de 71 a
96, enquanto as que ele errou marcaram 16 e 35. E como um clã repete os mesmos
nomes e origens em milhares de baús, um texto já cruzado pelos dois motores não
é conferido de novo: na prática, nenhuma conferência depois do primeiro lote.

Comparação no painel real (4 baús, todos com 100% de acerto):

| motor | tempo |
|---|---|
| **Tesseract** (por, psm 6, oem 1) | **310 ms** |
| RapidOCR (4 threads, sem classificador) | 462 ms |
| onnxtr `fast_tiny` | 1040 ms |
| easyocr | não testado — exigiria ~2,5 GB de PyTorch |

### Velocidade

Medido no jogo real, por baú: **2278 ms no início, 212 ms agora** — mil baús
caem de 38 para 3,5 minutos. O que mudou:

| mudança | efeito |
|---|---|
| ler os 4 baús da tela numa passagem só | o motor custa o mesmo para 4 que para 1 |
| Tesseract na frente, RapidOCR sob demanda | 462 → 310 ms, e depois nem isso |
| memória de leituras já conferidas | zero conferências após o primeiro lote |
| Tesseract com um idioma só | 446 → 310 ms |
| 4 threads no RapidOCR | 1646 → 375 ms (8 threads pioram: 1097 ms) |
| sem o classificador de ângulo | −490 ms (texto de jogo não gira) |
| `capture_scale` 3.0 → 2.0 | captura 477 → 263 ms, lê igual |

Se quiser acelerar mais, o parâmetro **Pausa entre baús** (`timing.chest_click_delay`,
0,25 s) é o que sobra: são 4 cliques por lote. Como os cliques vão de baixo para
cima — abrir um baú só desloca os que estão abaixo dele — não é preciso esperar a
lista assentar entre um e outro, então dá para baixá-lo com segurança.

### O banco corrige o OCR

Um clã é um mundo fechado: algumas centenas de jogadores, uns noventa tipos de
baú, uma centena de origens — tudo já em `collected_chests`, `members` e
`player_name_mappings`. Antes de coletar um perfil, o coletor carrega essas
listas (634 ms uma vez, contra 261 mil baús) e **reconhece** cada campo lido em
vez de aceitá-lo cru.

**O que o reconhecimento pode fazer — e só isso:**

1. aplicar uma correção que uma pessoa já cadastrou em `player_name_mappings`;
2. resolver diferença de **formatação** contra uma grafia já conhecida — mesmas
   letras e dígitos, diferindo em espaço, pontuação ou maiúsculas:
   `AncientWarrior'sChest` é `Ancient Warrior's Chest`, `|IMPERATOR` é
   `IMPERATOR`.

**O que ele não pode fazer: decidir que um nome é outro por serem parecidos.**
Isso foi tentado e removido. Com semelhança de 0,88, neste banco,
`Common Chest of Wealth` casa com `Uncommon Chest of Wealth` (0,957),
`DaNyx Darkher` com `Nyx Darkher` (0,917) e `Pandeménia` com `Pandeménio`
(0,900) — entidades diferentes.

E os dois erros não são simétricos: um nome gravado errado **aparece** nos
relatórios, e a interface web junta os dois jogadores, gravando a correção em
`player_name_mappings` para não repetir. Dois jogadores fundidos pelo coletor
não deixam rastro para identificar nem desfazer. Por isso **nome desconhecido é
gravado exatamente como foi lido**.

O reconhecimento continua servindo à **velocidade**: três campos já conhecidos
dispensam o motor lento.

Os vocabulários têm hierarquia: **tabela de referência primeiro, histórico
depois**. Para origens, a referência é `standard_chests` — que é onde a pontuação
se apoia, então uma origem escrita de outro jeito não pontua. Para jogadores, são
`members` e as correções manuais. O histórico de `collected_chests` cobre o que a
referência não lista (18 origens em produção, com milhares de baús). Nomes de baú
não têm tabela de referência e seguem por frequência.

A escolha é feita depois de tudo carregado, não linha a linha, para não depender
da ordem — e isso importou: `standard_chests` lista `Hermes' Store` e
`Hermes’ Store`, diferindo só no apóstrofo. Entre duas grafias oficiais decide o
uso no histórico (746 baús contra 88). Onde existe, a coluna `alias` também é
respeitada: `Doomsday` resolve para `Epic Undead squad`.

Validado contra a produção: as **86 correções feitas à mão continuam sendo
aplicadas** e, do histórico inteiro, apenas 7 grafias mudam — todas de formatação
(`Old Silver Wolf II]` → `Old Silver Wolf II`, `MM C` → `MMC`).

**Quando o vocabulário é lido:** uma vez por perfil, no início da coleta (530 ms
contra 261 mil baús), não a cada baú. Ele ainda **aprende durante a execução** —
um membro novo é desconhecido só no primeiro baú dele, não nos cinquenta que
mandar num evento. As consultas que sobram por baú (o mapeamento e o INSERT)
custam 0,244 ms contra 238 ms de OCR, ou seja 0,1%: não vale agrupar, e o commit
por baú é o que garante que uma queda no meio não perca o já coletado.

**Nomes que diferem só no número não se misturam:** o clã tem `Willykins`,
`Willykins 1` e `Willykins 11` como três pessoas. Quando as letras batem e os
dígitos não, a correspondência é recusada — um membro novo fica desconhecido em
vez de virar outro.

**Ordem de autoridade:** `player_name_mappings` primeiro — são correções que uma
pessoa decidiu, inclusive casos que nenhum algoritmo acerta (`Gwenllyian?` é
`Gwenllyian` ou `Gwenllyian³`?). O vocabulário só responde o que a tabela não
responde. E a gravação continua passando pela tabela, como sempre passou.

### Perfis com nomes parecidos

`Crash BR` e `Cash BR` se parecem o bastante para qualquer limiar aceitar os
dois. Em vez de um teste "esse nome é o que procuro?", os nomes **competem**: o
texto lido é comparado com todos os perfis da conta e só vence quem ganhar dos
outros com folga. Empate é tratado como leitura ambígua — o que é recuperável —
em vez de virar o perfil errado, que não é.

---

## Onde cada problema é registrado

Duas naturezas diferentes, dois destinos:

**Problema de dado** — um baú que não deu para ler. Vai para
`incomplete_chests`, com a captura da tela, e é resolvido por uma pessoa na tela
de revisão. Tem decisão humana por trás, então tem lugar no banco.

**Falha de execução** — o menu não abriu, o banco recusou o INSERT, o clique
parou de consumir baús, um perfil não foi alcançado. Fica **só no log local**
(`execution_logs/collector_AAAA-MM-DD.log`), porque ninguém resolve isso por uma
tela: é operação, não dado.

A tabela `errors` está reservada para problemas que a interface venha a tratar e
**não é escrita pelo coletor**. Usá-la para falhas de execução a transformaria
numa cópia pior do arquivo de log.

### Baú que não deu para ler vira fila de revisão

Um baú ilegível não interrompe mais a coleta: o **recorte da tela** é gravado em
`incomplete_chests.screenshot` (PNG, ~115 KB) e o baú é aberto como qualquer
outro. Antes ele ficava no jogo para ser lido de novo — sempre igual de ilegível.

Na interface web, **Admin › Chests › Incomplete Chests** (com contador de
pendentes) mostra a fila. Cada registro abre com a imagem ao lado do formulário;
quem revisa lê o que o OCR não leu e ou **corrige** — o que grava em
`collected_chests` **com a data original da coleta**, para contar no ciclo certo —
ou marca como **analisado e não recuperável**, com nota.

O registro nunca é apagado: fica marcado com o resultado e, quando corrigido,
apontando para o `collected_chests` que gerou. Há ainda "reabrir", para desfazer
uma decisão.

As colunas vieram por migration (`AddScreenshotAndReviewToIncompleteChests`) no
banco atual, e por `ALTER TABLE` equivalente no antigo, cuja instância web não
usa migrations — os dois têm a mesma estrutura. Ainda assim o coletor verifica a
coluna antes de usá-la e grava sem imagem se ela faltar, em vez de falhar.

O baú recuperado por uma pessoa é gravado com `type = 1` (*Manual*), como o
schema já previa: dá para separar depois o que o coletor leu do que alguém
reconstruiu a partir da imagem.

**Painel vazio não encerra a coleta na hora.** O jogo repõe a lista um instante
depois que os últimos baús são abertos, e uma captura tirada nesse intervalo
mostra a tela vazia — foi assim que uma execução parou com baús ainda esperando.
Agora o coletor espera 1 s e olha de novo; só termina se continuar vazio.

A ordem é ler, gravar, depois clicar. Se o clique falhar, o baú é lido de novo na
próxima execução e pode duplicar — duplicata aparece e se corrige; baú perdido,
não.

## Estrutura

```
config/     config.json (todos os parâmetros) · calibration.json · schema.py
core/       browser (CDP) · ocr · vision · calibration · session · runner
modules/    chest_collector · journal_parser · chat_automator
gui/        app (configuração) · calibration_wizard
database/   conexão e repositórios MySQL
```

`config/schema.py` descreve cada parâmetro uma única vez; a interface, os
valores padrão e a validação saem dele. Um parâmetro novo declarado lá aparece
na tela sozinho.

---

## Arquivos de configuração

| Arquivo | Conteúdo | Versionado? |
|---|---|---|
| `config/config.json` | contas, senhas, bancos, todos os parâmetros | não (senhas) |
| `config/calibration.json` | posições da tela desta máquina | não |
| `config/calib_refs/*.png` | imagens de referência da calibração | não |

As senhas ficam em texto no `config.json`, como já ficavam no `position.cfg`. O
arquivo está no `.gitignore`; proteja a pasta se a máquina for compartilhada.
