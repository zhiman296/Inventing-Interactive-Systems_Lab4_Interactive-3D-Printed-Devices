# Making Pottery
- A 3D-printed container("the pottery") has two photoresistors built into its sides. 
- Your hands change how much light reaches each sensor. 
- An ESP32 reads the sensors and sends the values to the game, shows a clay pot turning on a pottery wheel and shapes it as you move.

## How it works
Left hand	-> Left photoresistor	-> Moves the shaping cursor up and down the pot (more covered = higher)

Right hand ->	Right photoresistor -> Squeezes the clay thinner at the cursor (more covered = thinner)

- Each photoresistor is part of a voltage divider, so the ESP32 can read it as a number from 0 to 4095 (lower = darker).
- The ESP32 sends one line every ~20 ms, for example 2310,1984 (left, right).
- The pygame program turns those numbers into cursor height and squeeze strength, using a short calibration at the start.

## Playing
- Calibration (first run, or press C)
- Step 1: keep your hands away from both sensors and press SPACE.
- Step 2: cover both sensors with your hands and press SPACE.
When "Calibrated" appears, press SPACE again to start.

Calibration is saved in pottery_calibration.json. Calibrate again whenever the room light changes.

## Controls
### Key	Action
- C : Calibrate again
- SPACE : Push the clay outward (hold)
- R	: New lump of clay
- S	: Save a picture
- ESC :	Quit
