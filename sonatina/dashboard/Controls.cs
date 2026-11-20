using Microsoft.UI.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Shapes;
using Windows.Foundation;

namespace SonatinaDashboard;

/// <summary>Lays children out left to right and wraps (codec chips).</summary>
public sealed class WrapPanel : Panel
{
    public double Spacing { get; set; } = 8;

    protected override Size MeasureOverride(Size availableSize)
    {
        double x = 0, y = 0, row = 0, widest = 0;
        foreach (var child in Children)
        {
            child.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
            var size = child.DesiredSize;
            if (x > 0 && x + size.Width > availableSize.Width)
            {
                y += row + Spacing;
                x = 0;
                row = 0;
            }
            x += size.Width + Spacing;
            row = Math.Max(row, size.Height);
            widest = Math.Max(widest, x - Spacing);
        }
        double width = double.IsInfinity(availableSize.Width) ? widest : Math.Min(widest, availableSize.Width);
        return new Size(width, y + row);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        double x = 0, y = 0, row = 0;
        foreach (var child in Children)
        {
            var size = child.DesiredSize;
            if (x > 0 && x + size.Width > finalSize.Width)
            {
                y += row + Spacing;
                x = 0;
                row = 0;
            }
            child.Arrange(new Rect(x, y, size.Width, size.Height));
            x += size.Width + Spacing;
            row = Math.Max(row, size.Height);
        }
        return finalSize;
    }
}

/// <summary>Microphone level: 24 bars, green up to -6 dBFS, red above.</summary>
public sealed class LevelMeter : Grid
{
    private const int Bars = 24;
    private readonly Border[] _bars = new Border[Bars];
    private int _lit = -1;

    public LevelMeter()
    {
        ColumnSpacing = 3;
        Height = 10;
        for (int i = 0; i < Bars; i++)
        {
            ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var bar = new Border { CornerRadius = new CornerRadius(2) };
            SetColumn(bar, i);
            Children.Add(bar);
            _bars[i] = bar;
        }
        SetLevel(0);
    }

    /// <summary>level: linear peak 0..1.</summary>
    public void SetLevel(double level)
    {
        int lit = 0;
        if (level > 0.0005)
        {
            double db = 20 * Math.Log10(level);              // -66 .. 0
            lit = (int)Math.Round((db + 48) / 48 * Bars);
            lit = Math.Clamp(lit, 1, Bars);
        }
        if (lit == _lit)
            return;
        _lit = lit;
        var off = Ui.Res("ControlStrongFillColorDisabledBrush");
        var green = Ui.Res("SystemFillColorSuccessBrush");
        var red = Ui.Res("SystemFillColorCriticalBrush");
        for (int i = 0; i < Bars; i++)
            _bars[i].Background = i < lit ? (i < Bars - 3 ? green : red) : off;
    }
}

/// <summary>Coloured status chip ("In riproduzione", "In chiamata").</summary>
public sealed class Pill : Grid   // Border is sealed in WinUI 3; Grid has the same border/corner/padding properties
{
    private readonly Ellipse _dot = new() { Width = 6, Height = 6, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _text = new() { FontSize = 12, LineHeight = 16, FontWeight = FontWeights.SemiBold };

    public Pill()
    {
        CornerRadius = new CornerRadius(12);
        Padding = new Thickness(10, 2, 10, 3);
        VerticalAlignment = VerticalAlignment.Center;
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        row.Children.Add(_dot);
        row.Children.Add(_text);
        Children.Add(row);
        Set("", "neutral");
    }

    /// <summary>kind: success | attention | caution | critical | neutral.</summary>
    public void Set(string text, string kind)
    {
        var (fg, bg) = kind switch
        {
            "success" => ("SystemFillColorSuccessBrush", "SystemFillColorSuccessBackgroundBrush"),
            "attention" => ("SystemFillColorAttentionBrush", "SystemFillColorAttentionBackgroundBrush"),
            "caution" => ("SystemFillColorCautionBrush", "SystemFillColorCautionBackgroundBrush"),
            "critical" => ("SystemFillColorCriticalBrush", "SystemFillColorCriticalBackgroundBrush"),
            _ => ("TextFillColorSecondaryBrush", "SubtleFillColorSecondaryBrush"),
        };
        _text.Text = text;
        _text.Foreground = Ui.Res(fg);
        _dot.Fill = Ui.Res(fg);
        Background = Ui.Res(bg);
        Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
    }
}

/// <summary>Automatico / Sempre attivo / Disattivato, as a segmented control.</summary>
public sealed class ModePicker : Grid
{
    public static readonly (string Key, string It, string En)[] Modes =
    [
        ("auto", "Automatico", "Automatic"),
        ("on", "Sempre attivo", "Always on"),
        ("off", "Disattivato", "Off"),
    ];

    private readonly List<ToggleButton> _buttons = [];
    public string Mode { get; private set; } = "";
    public event Action<string>? Picked;

    public ModePicker()
    {
        CornerRadius = new CornerRadius(6);
        Padding = new Thickness(3);
        BorderThickness = new Thickness(1);
        BorderBrush = Ui.Res("CardStrokeColorDefaultBrush");
        Background = Ui.Res("ControlAltFillColorSecondaryBrush");
        HorizontalAlignment = HorizontalAlignment.Left;
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 2 };
        foreach (var (key, it, en) in Modes)
        {
            var button = new ToggleButton
            {
                Content = L.T(it, en),
                Tag = key,
                MinWidth = 96,
                Padding = new Thickness(14, 5, 14, 6),
                CornerRadius = new CornerRadius(4),
            };
            button.Click += (_, _) =>
            {
                bool changed = key != Mode;
                Set(key);
                if (changed)
                    Picked?.Invoke(key);
            };
            _buttons.Add(button);
            row.Children.Add(button);
        }
        Children.Add(row);
    }

    public void Set(string mode)
    {
        Mode = mode;
        foreach (var button in _buttons)
            button.IsChecked = (string)button.Tag == mode;
    }
}
