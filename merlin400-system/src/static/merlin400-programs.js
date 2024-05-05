var AppContext = AppContext || {};

AppContext.Programs = [
    {
      id: 1,
      name: "Make Extract",
      description:
        "Performs a full extraction of the cannabis and removes the alcohol. The extraction does not decarboxylate the material",
      icon: "close_fullscreen",
      color: "var(--drizzle-red)",
      soakTimeDefault: 30,
      statusLabels: [
        {
          minValue: 0.2,
          label: "Initializing",
        },
        {
          minValue: 0.4,
          label: "Extracting",
        },
        {
          minValue: 0.6,
          label: "Distilling",
        },
        {
          minValue: 0.8,
          label: "Finishing",
        },
      ],
    },
    {
      id: 2,
      name: "Decarboxylate",
      description:
        "Decarboxylates the extracted oil. Please run this program after the oil has been extracted and purified",
      icon: "build",
      color: "var(--drizzle-blue)",
      statusLabels: [
        {
          minValue: 0.0,
          label: "Starting",
        },
        {
          minValue: 0.1,
          label: "Decarboxylating",
        },
      ],
    },
    {
      id: 3,
      name: "Heat for Mixing",
      description: "Heats the oil to 50 degrees so you can mix it with olive oil or siphon it off with a pipette.",
      icon: "invert_colors",
      color: "var(--drizzle-orange)",
      statusLabels: [],
    },
    {
      id: 4,
      name: "Distillation Only",
      description: "Distills the alcohol present in the distiller. Does not perform an extraction.",
      icon: "invert_colors",
      color: "var(--drizzle-green)",
      statusLabels: [],
    },
    {
      id: 5,
      name: "Extract Only",
      description: "Extacts the material in the machine, but does not remove the alcohol afterwards.",
      icon: "invert_colors  ",
      color: "var(--drizzle-purple)",
      statusLabels: [],
    },
    {
      id: 6,
      name: "Vent Pump",
      description:
        "Vents the pump and blows air through it in case the pump is contaminated and not running smooth.",
      icon: "invert_colors",
      color: "var(--drizzle-purple)",
      statusLabels: [],
    }
  ];