# Manual paper book and strategy builder

Status: planned. Not implemented. Do not deploy this until it is built and asked for.

Paper only. The book never sends a Kite order. It is one more independent paper book on the main page, with its own ledger.

## What the page does today

The main option chain is the nearest Nifty expiry: open interest, change, last price, and strike. There is no buy or sell on a cell. The instruments file already lists later expiries. The live feed keeps the nearest chain, plus the next-week at-the-money pair. Each existing paper book has its own ledger. None of them is a manual multi-leg book.

## The book

Name: Manual. On by default. Outside the regime policy and the kill switch.

A draft is a list of legs. Paper trade turns that draft into one open strategy. Each leg stores:

- side: buy or sell
- instrument: call, put, or the nearest future
- expiry
- strike (blank for the future)
- lots
- fill price
- charges

Fills use the live last price and the same NFO charge model as the other paper books. Closing one leg, or the whole strategy, writes a close with charges. The ledger is its own file, separate from iron fly, short straddle, agent, and the rest.

A naked short is marked unlimited. A defined-risk shape stores max profit, max loss, and breakevens from the leg strikes and the net premium.

## Chain

Each call and each put cell gets B and S. A click adds that leg to the draft at the price on the screen. It does not fill. A four-leg condor can be assembled before anything is paper-traded.

The expiry picker sits on the chain bar. The chain shows one expiry at a time, nearest first, then the later expiries from the instruments file. Changing expiry redraws that chain. Legs already in the draft stay, including legs from another expiry.

The socket subscribes to the expiry on screen, plus any draft or open leg from another expiry. It does not subscribe to every strike of every expiry.

## Strategy builder

The builder sits with the chain. It is the draft, the totals, and the ready-made menu.

Leg rows show side, expiry, strike, call or put or future, lots, and price. The user can edit, delete, shift strikes, and set width. Totals show premium paid or received, charges, max profit, max loss when the risk is defined, and breakevens.

Paper trade fills the whole draft. Reset clears the draft only.

Ready-made tiles only fill the draft. Each tile is a fixed recipe: side, call or put or future, strike counted in steps from the at-the-money strike, lot ratio, and whether the leg uses this expiry or the next one. Shift and Width still edit the draft before the paper trade. A missing quote leaves that leg blank until the chain has a price.

Ratio templates use 1 and 2 lots. Calendar templates use the selected expiry and the next expiry at the same strike. Futures templates use the nearest future.

### Bullish

- Buy Call
- Sell Put
- Bull Call Spread
- Bull Put Spread
- Call Ratio Back Spread (sell 1, buy 2)
- Long Calendar with Calls
- Bull Condor
- Bull Butterfly
- Range Forward
- Buy Future
- Long Synthetic Future (buy the call, sell the put, same strike)

### Bearish

- Buy Put
- Sell Call
- Bear Call Spread
- Bear Put Spread
- Put Ratio Back Spread (sell 1, buy 2)
- Long Calendar with Puts
- Bear Condor
- Bear Butterfly
- Risk Reversal
- Sell Future
- Short Synthetic Future (sell the call, buy the put, same strike)

### Neutral

- Short Straddle
- Short Strangle
- Iron Butterfly
- Short Iron Condor
- Batman
- Double Plateau
- Jade Lizard
- Reverse Jade Lizard

### Others

- Long Straddle
- Long Strangle
- Long Iron Butterfly
- Long Iron Condor
- Call Ratio Spread (buy 1, sell 2)
- Put Ratio Spread (buy 1, sell 2)
- Strip (buy 1 call and 2 puts at the same strike)
- Strap (buy 2 calls and 1 put at the same strike)

### NSE workbook tiles

Sensibull’s list already covers the long and short call and put, both straddles and strangles, the four vertical spreads, and the call butterfly and call condor. These use the nearest future and are their own tiles:

- Covered Call (long future, short call)
- Protective Put (long future, long put)
- Protective Call (short future, long call)
- Collar (long future, long put, short call)

Also include the put-side pair from the same workbook:

- Long Put Butterfly
- Short Put Butterfly
- Long Put Condor
- Short Put Condor

### Named shapes

- Batman: a call butterfly above the spot and a put butterfly below it, so the payoff has two humps.
- Double Plateau: a put condor below the spot and a call condor above it, so the payoff has two flat zones.
- Jade Lizard: a short put plus a short call spread.
- Reverse Jade Lizard: a short call plus a short put spread.

A custom draft can still be any mix of listed calls, puts, and the future. The menu is the union of the Sensibull ready-made tabs and the NSE options-workbook names. It is not a short sample of four shapes.

## Payoff

Two curves.

The expiry curve is the exact payoff at expiry. The target-date curve is the model value on the date the user picks, from each leg’s implied volatility, falling back to at-the-money volatility when a leg has none. Open interest sits behind the chart. A spot line marks the last price.

The date control runs from today to the furthest expiry in the draft. Hovering a point shows the spot, the expiry profit, the target-date profit, and the per-leg value, and the readout stays readable. The same graph can show the draft or an open strategy.

## Positions and later analysis

The open list shows each strategy, the live mark, profit since fill, and charges so far. The user can close one leg or close the strategy. The closed list keeps the result.

Opening a strategy again loads it into the graph with the target-date control, the summed greeks already on the feed, and the breakevens. Closes from this book appear in the existing paper-trades list so the day’s score still adds up.

## What this will not copy

Sensibull’s probability-of-profit percentage and their span margin come from their own model. This book can show a simple probability from the volatility, and it shows premium, charges, and defined max loss. It does not reproduce their probability figure or their margin figure.

## Build order

1. The book, the chain B and S buttons, a one-expiry draft, fill, and close.
2. The payoff, the hover readout, and the target date.
3. The expiry picker and legs from more than one expiry.
4. The full ready-made menu, including futures legs, ratios, and calendars.
5. Position analysis on an open strategy.

No deploy until asked.
