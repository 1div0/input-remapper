# The problems with overwriting keys

Branches for some of that stuff exist to archive it instead of loosing it
forever.

**Initial target** You write a symbols file based on your specified mapping,
and that's pretty much it. There were two mappings: The first one is in the
keycodes file and contains "<10> = 10", which is super redundant but needed
for xkb. The second one mapped "<10>" to characters, modifiers, etc. using
symbol files in xkb. However, if you had one keyboard layout for your mouse
that writes SHIFT keys on keycode 10, and one for your keyboard that is normal
and writes 1/! on keycode 10, then you would not be able to write ! by
pressing that mouse button and that keyboard button at the same time.

This was quite mature, pretty much finished and tested.

**The second idea** was to write special keycodes known only to key-mapper
(256 - 511) into the input device of your mouse in /dev/input, and map
those to SHIFT and such, whenever a button is clicked. A mapping would have
existed to prevent the original keycode 10 from writing a 1. But Linux seems
to ignore anything greater than 255 for regular keyboard events (EDIT: I think
this is because of the device capabilities), or even crash X in some cases when 
something is wrong with the configuration.

**The third idea** is to create a new input device that uses 8 - 255, just
like other layouts, and key-mapper always tries to use the same keycodes for
SHIFT as already used in the system default. The pipeline is like this:

1. A human thumb presses an extra-button of the device "mouse"
2. key-mapper uses evdev to get the event from "mouse", sees "ahh, it's a
   10, I know that one and will now write 50 into my own device". 50 is
   the keycode for SHIFT on my regular keyboard, so it won't clash anymore
   with alphanumeric keys and such.
3. X has key-mappers configs for the key-mapper device loaded and
   checks in it's keycodes config file "50, that would be <50>", then looks
   into it's symbols config "<50> is mapped to SHIFT", and then it actually
   presses the SHIFT down to modify all other future buttons.
4. X has another config for "mouse" loaded, which prevents any system default
   mapping to print the overwritten key "1" into the session.
   
But this is a rather complicated approach. The mapping of 10 -> 50 would
have to be stored somewhere as well. It would make the mess of configuration
files already needed for xkb even worse. This idea was not considered for
long, so no "third" branch exists.

**Fourth idea**: Based on the first idea, instead of using keycodes greater
than 255, use unused keycodes starting from 255, going down. Issues existed
when two buttons with the same keycode are pressed at the same time,
so the goal is to avoid such overlaps. For example, if keycode 10 should be
mapped to Shift_L. It is impossible to write "!" using this mapped button
and a second keyboard, except if pressing key 10 triggers key-mapper to write
key 253 into the /dev device, while mapping key 10 to nothing. Unfortunately
linux just completely ignores some keycodes. 140 works, 145 won't, 150 works.
EDIT: I think this is because of "capabilities", however, injecting keycodes
won't work at all anyway, see the fifth idea.

**Fifth idea**: Instead of writing xkb symbol files, just disable all
mouse buttons with a single symbol file. Key-mapper listens for key events
in /dev and then writes the mapped keycode into a new device in /dev. For
example, if 10 should be mapped to Shift_L, xkb configs would disable
key 10 and key-mapper would write 50 into /dev, which is Shift_L in xmodmaps
output. This sounds incredibly simple and makes me throw away tons of code.
This conflicts with the original keycodes though, writing custom keycodes
into /dev/uinput makes the original keycode not mapped by xbk symbol files,
and therefore leak through. In the previous example, it would still write '1',
and then after that the other key. By adding a timeout single keys work, but
holding down a button that is mapped to shift will (would usually have
a keycode of 10, now triggers writing 50) write "!!!!!!!!!". Even though
no symbols are loaded for that button.

This is because the second device that starts writing an event.value of 2 will
take control of what is happening. Following example: (KB = keyboard,
example devices)
1. hold a on KB1: `a-1`, `a-2`, `a-2`, `a-2`, ...
2. hold shift on KB2: `shift-2`, `shift-2`, `shift-2`, ...
No a-2 on KB1 happening anymore. The xkb symbols of KB2 will
be used! So if KB2 maps shift+a to b, it will write b, even
though KB1 maps shift+a to c! And if you reverse this, hold
shift on KB2 first and then a on KB1, the xkb mapping of KB1
will take effect and write c!

Which means in order to prevent "!!!!!!" being written while holding down
keycode 10 on the mouse, which is supposed to be shift, the 10 of the
key-mapper /dev node has to be mapped to none as well. But that would
prevent a key that is mapped to "1", which translates to 10, from working.

That means instead of using the output from xmodmap to determine the correct
keycode, use a custom mapping that starts at 255 and just offsets xmodmap
by 255. The correct capabilities need to exist this time. Everything below
255 is disabled. This mapping is applied to key-mappers custom /dev node.


# How I would have liked it to be

setxkbmap -layout ~/.config/key-mapper/mouse -device 13

config looks like:
```
10 = a, A
11 = Shift_L
```

done. Without crashing X. Without printing generic useless errors. Without
colliding with other devices using the same keycodes. If it was that easy,
an app to map keys would have already existed.
